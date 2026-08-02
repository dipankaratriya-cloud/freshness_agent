"""
BigQuery I/O for staleness_pipeline_v2 — deliberately separate from the
shared root bq.py (that module belongs to the other, already-running
production pipeline; this one is its own Cloud Run Job with its own table).

Input:  SOURCE_QUERY reads the live provenance graph (Edge/Node join filtered
        to predicate='sourceDataUrl') from datcom-store, owned by the data
        engineering team.
Output: data_freshness_report in datcom-import-dev-768877.dc_kg_dashboard — a
        shared dashboard table (this pipeline is one of potentially several
        writers into it). Append-only: one row per dataset per run. Full
        per-URL logs live in GCS (see gcs_io.py); this table stores only the
        gs:// URI pointing at each one, not the log text itself.

NOTE: the billing project (whichever project runs the query job) needs
`roles/bigquery.jobUser` there, AND the identity needs read access on
datcom-store specifically — that grant must come from datcom-store's own
project owner, not from anything here. Separately, datcom-import-dev-768877
needs its own BigQuery write access, granted independently of datcom-store.
"""

import os
from collections import defaultdict
from datetime import date, datetime, timezone

from google.cloud import bigquery

from specialized_source_handlers import normalize_url

BQ_DATASET = os.environ.get("BQ_DATASET", "dc_kg_dashboard")
TABLE_NAME = "data_freshness_report"
_client_cache: dict[str, bigquery.Client] = {}

# Hard-coded per the data-engineering team's confirmation that this staging
# dataset is stable, not periodically re-dated — update manually if that changes.
SOURCE_QUERY = """
SELECT edge.predicate, edge.object_id, node.value
FROM `datcom-store.dc_graph_staging_2026_07_28.Edge` as edge
INNER JOIN `datcom-store.dc_graph_staging_2026_07_28.Node` as node
ON edge.object_id=node.subject_id
WHERE edge.predicate='sourceDataUrl'
"""

DATA_FRESHNESS_REPORT_SCHEMA = [
    bigquery.SchemaField("run_id",              "STRING"),
    bigquery.SchemaField("last_execution_date", "DATE"),
    bigquery.SchemaField("created_at",          "TIMESTAMP"),
    bigquery.SchemaField("updated_at",          "TIMESTAMP"),
    bigquery.SchemaField("serial_no",           "INT64"),
    bigquery.SchemaField("object_id",           "STRING"),
    bigquery.SchemaField("provenance_url",      "STRING"),
    bigquery.SchemaField("last_refresh_date",   "STRING"),
    bigquery.SchemaField("date_method",         "STRING"),
    bigquery.SchemaField("date_source",         "STRING"),
    bigquery.SchemaField("tier_used",           "INT64"),
    bigquery.SchemaField("date_found",          "BOOL"),
    bigquery.SchemaField("verification_steps",  "STRING"),
    bigquery.SchemaField("tiers_attempted",     "STRING"),
    bigquery.SchemaField("tier_failed_reason",  "STRING"),
    bigquery.SchemaField("time_to_execute_sec", "FLOAT64"),
    bigquery.SchemaField("log_gcs_uri",         "STRING"),
]


def _client(billing_project: str) -> bigquery.Client:
    if billing_project not in _client_cache:
        _client_cache[billing_project] = bigquery.Client(project=billing_project)
    return _client_cache[billing_project]


def _table(billing_project: str) -> str:
    return f"{billing_project}.{BQ_DATASET}.{TABLE_NAME}"


def load_urls_from_bq(billing_project: str) -> dict[str, list[str]]:
    """Runs SOURCE_QUERY and returns {url: [dataset_id, ...]} — same shape
    and same dedup logic as staleness_pipeline_v2.load_urls() uses for the
    CSV path, so behavior is identical once this can actually reach
    datcom-store."""
    client = _client(billing_project)
    url_map: dict[str, list[str]] = defaultdict(list)
    for row in client.query(SOURCE_QUERY).result():
        raw_url = (row.value or "").strip().strip('"')
        oid = row.object_id
        if not raw_url:
            continue
        for url in normalize_url(raw_url):
            if url.startswith("http") and oid not in url_map[url]:
                url_map[url].append(oid)
    return url_map


def ensure_table(billing_project: str) -> None:
    client = _client(billing_project)
    ds = bigquery.Dataset(f"{billing_project}.{BQ_DATASET}")
    ds.location = "US"
    client.create_dataset(ds, exists_ok=True)

    table_ref = _table(billing_project)
    try:
        existing = client.get_table(table_ref)
        existing_fields = {f.name for f in existing.schema}
        new_fields = [f for f in DATA_FRESHNESS_REPORT_SCHEMA if f.name not in existing_fields]
        if new_fields:
            existing.schema = list(existing.schema) + new_fields
            client.update_table(existing, ["schema"])
            print(f"  [bq_io] schema updated: {TABLE_NAME} (+{len(new_fields)} new columns)")
        else:
            print(f"  [bq_io] table ready: {table_ref}")
    except Exception:
        t = bigquery.Table(table_ref, schema=DATA_FRESHNESS_REPORT_SCHEMA)
        t.time_partitioning = bigquery.TimePartitioning(field="last_execution_date")
        t.clustering_fields = ["object_id"]
        client.create_table(t, exists_ok=True)
        print(f"  [bq_io] table created: {table_ref}")


def write_results(billing_project: str, run_id: str, rows: list[dict]) -> None:
    """rows: the same per-dataset dicts staleness_pipeline_v2.main() builds
    for the CSV, plus a 'log_gcs_uri' key (see gcs_io.upload_log)."""
    if not rows:
        return
    today = str(date.today())
    now = datetime.now(timezone.utc).isoformat()
    payload = [
        {
            "run_id":              run_id,
            "last_execution_date": today,
            "created_at":          now,
            "updated_at":          now,
            "serial_no":           r.get("serial_no"),
            "object_id":           r.get("object_id", ""),
            "provenance_url":      r.get("url", ""),
            "last_refresh_date":   r.get("last_refresh_date"),
            "date_method":         r.get("date_method"),
            "date_source":         r.get("date_source"),
            "tier_used":           r.get("tier_used"),
            "date_found":          bool(r.get("date_found")),
            "verification_steps":  r.get("verification_steps"),
            "tiers_attempted":     r.get("tiers_attempted", ""),
            "tier_failed_reason":  r.get("tier_failed_reason"),
            "time_to_execute_sec": r.get("extraction_time_sec"),
            "log_gcs_uri":         r.get("log_gcs_uri", ""),
        }
        for r in rows
    ]
    errors = _client(billing_project).insert_rows_json(_table(billing_project), payload)
    if errors:
        print(f"  [bq_io] insert errors (showing first 3): {errors[:3]}")
    else:
        print(f"  [bq_io] ✓ {len(payload)} rows -> {TABLE_NAME}")
