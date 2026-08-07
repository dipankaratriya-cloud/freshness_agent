"""
BigQuery I/O for staleness_pipeline_v2 — deliberately separate from the
shared root bq.py (that module belongs to the other, already-running
production pipeline; this one is its own Cloud Run Job with its own table).

Input:  SOURCE_QUERY reads the live provenance graph from datcom-store, owned
        by the data engineering team — an Edge/Node join against the PROD
        dataset (spanner_dc_graph_prod_DEFAULT), fetching both the
        sourceDataUrl edge's own value and its sibling `url` edge's value
        (joined as two edges off the SAME parent entity, not chained through
        the object — validated live this session: 429 sourceDataUrl edges,
        432 output rows after a small, accepted join fan-out on ~5 entities
        that have more than one `url` edge; not deduped here by design).
Output: Freshness_results in datcom-import-dev-768877.dc_kg_dashboard —
        this pipeline's own table (the older data_freshness_report table is
        no longer written to as of the 5-project quota fan-out; historical
        rows there are untouched but this is the sole output going forward).
        Append-only: one row per dataset per run. Full per-URL logs live in
        GCS under gs://freshness_logs/<run folder>/ (see gcs_io.py); this
        table stores only the gs:// URI pointing at each one, not the log
        text itself. Schema has exactly one run-identifying column
        (run_timestamp, TIMESTAMP) rather than the old run_id/created_at/
        updated_at/last_execution_date spread, and leads with the three
        columns straight from SOURCE_QUERY (provenance, sourcedataurl,
        provenance_url) plus url_used — the primary columns a dashboard
        consumer needs first.

NOTE: the billing project (whichever project runs the query job) needs
`roles/bigquery.jobUser` there, AND the identity needs read access on
datcom-store specifically (the spanner_dc_graph_prod_DEFAULT dataset,
not the old staging one) — that grant must come from datcom-store's own
project owner, not from anything here. Separately, datcom-import-dev-768877
needs its own BigQuery write access, granted independently of datcom-store.
"""

import os
from dataclasses import dataclass, field
from datetime import datetime

from google.cloud import bigquery

from specialized_source_handlers import normalize_url

BQ_DATASET = os.environ.get("BQ_DATASET", "dc_kg_dashboard")
TABLE_NAME = "Freshness_results"
_client_cache: dict[str, bigquery.Client] = {}

# Switched from the staging dataset (dc_graph_staging_2026_07_28) to prod
# (spanner_dc_graph_prod_DEFAULT) this session, and extended to also fetch
# the sibling `url` edge (provenance_url) alongside sourceDataUrl — see
# module docstring for the join-shape rationale and the accepted fan-out.
SOURCE_QUERY = """
SELECT
  e1.subject_id AS provenance,
  n1.value AS sourcedataurl,
  n2.value AS provenance_url
FROM `datcom-store.spanner_dc_graph_prod_DEFAULT.Edge` AS e1
INNER JOIN `datcom-store.spanner_dc_graph_prod_DEFAULT.Node` AS n1
  ON e1.object_id = n1.subject_id
LEFT JOIN `datcom-store.spanner_dc_graph_prod_DEFAULT.Edge` AS e2
  ON e2.subject_id = e1.subject_id AND e2.predicate = 'url'
LEFT JOIN `datcom-store.spanner_dc_graph_prod_DEFAULT.Node` AS n2
  ON e2.object_id = n2.subject_id
WHERE e1.predicate = 'sourceDataUrl'
"""


@dataclass
class ProvenanceEntity:
    """One row from SOURCE_QUERY. entity_id is e1.subject_id — the true
    provenance/dataset entity id (NOT the old object_id, which was the id of
    the literal node holding the sourceDataUrl string). Neither entity_id nor
    either candidate list is guaranteed unique across rows (see the accepted
    fan-out above) — callers must tolerate repeats, not dedupe by key."""
    entity_id: str
    sourcedataurl_candidates: list[str] = field(default_factory=list)
    provenance_url_candidates: list[str] = field(default_factory=list)


# Column order is deliberate, not alphabetical/historical: the three columns
# straight from SOURCE_QUERY (provenance, sourcedataurl, provenance_url) come
# first — "provenance" holds the true entity id (e1.subject_id), replacing
# the old table's "object_id" name outright, not just its meaning — followed
# by run_timestamp (the one and only run-identifying column) and url_used
# (which of the two URLs above actually produced the result). Everything
# after that is secondary/diagnostic. source_summary sits next to
# date_method/date_source specifically for validation/transparency — one
# plain-English sentence naming exactly where the date was read from
# (verification_recipes.source_summary()), short enough to scan across many
# rows without opening the full verification_steps trace.
FRESHNESS_RESULTS_SCHEMA = [
    bigquery.SchemaField("provenance",          "STRING"),
    bigquery.SchemaField("sourcedataurl",       "STRING"),
    bigquery.SchemaField("provenance_url",      "STRING"),
    bigquery.SchemaField("run_timestamp",       "TIMESTAMP"),
    bigquery.SchemaField("url_used",            "STRING"),
    bigquery.SchemaField("serial_no",           "INT64"),
    bigquery.SchemaField("last_refresh_date",   "STRING"),
    bigquery.SchemaField("date_found",          "BOOL"),
    bigquery.SchemaField("date_method",         "STRING"),
    bigquery.SchemaField("date_source",         "STRING"),
    bigquery.SchemaField("source_summary",      "STRING"),
    bigquery.SchemaField("tier_used",           "INT64"),
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


def load_urls_from_bq(billing_project: str) -> list[ProvenanceEntity]:
    """Runs SOURCE_QUERY and returns one ProvenanceEntity per row. Neither
    entity_id nor either URL is deduped here — the ~5-entity join fan-out is
    accepted as-is (see module docstring), and staleness_pipeline_v2's
    per-URL task cache is what dedupes actual fetch work, not this loader."""
    client = _client(billing_project)
    entities: list[ProvenanceEntity] = []
    for row in client.query(SOURCE_QUERY).result():
        entity_id = row.provenance
        if not entity_id:
            continue

        sourcedataurl_raw = (row.sourcedataurl or "").strip().strip('"')
        sourcedataurl_candidates = (
            [u for u in normalize_url(sourcedataurl_raw) if u.startswith("http")]
            if sourcedataurl_raw else []
        )

        provenance_url_raw = (row.provenance_url or "").strip().strip('"')
        provenance_url_candidates = (
            [u for u in normalize_url(provenance_url_raw) if u.startswith("http")]
            if provenance_url_raw else []
        )

        if not sourcedataurl_candidates and not provenance_url_candidates:
            continue
        entities.append(ProvenanceEntity(
            entity_id=entity_id,
            sourcedataurl_candidates=sourcedataurl_candidates,
            provenance_url_candidates=provenance_url_candidates,
        ))
    return entities


def ensure_table(billing_project: str) -> None:
    client = _client(billing_project)
    ds = bigquery.Dataset(f"{billing_project}.{BQ_DATASET}")
    ds.location = "US"
    client.create_dataset(ds, exists_ok=True)

    table_ref = _table(billing_project)
    try:
        existing = client.get_table(table_ref)
        existing_fields = {f.name for f in existing.schema}
        new_fields = [f for f in FRESHNESS_RESULTS_SCHEMA if f.name not in existing_fields]
        if new_fields:
            existing.schema = list(existing.schema) + new_fields
            client.update_table(existing, ["schema"])
            print(f"  [bq_io] schema updated: {TABLE_NAME} (+{len(new_fields)} new columns)")
        else:
            print(f"  [bq_io] table ready: {table_ref}")
    except Exception:
        t = bigquery.Table(table_ref, schema=FRESHNESS_RESULTS_SCHEMA)
        t.time_partitioning = bigquery.TimePartitioning(field="run_timestamp")
        t.clustering_fields = ["provenance"]
        client.create_table(t, exists_ok=True)
        print(f"  [bq_io] table created: {table_ref}")


def write_results(billing_project: str, run_started_at: datetime, rows: list[dict]) -> None:
    """rows: the same per-entity dicts staleness_pipeline_v2.main() builds
    for the CSV, plus a 'log_gcs_uri' key (see gcs_io.upload_log).

    run_started_at is the single datetime shared by every row of one pipeline
    run (main() computes it once) — this table has exactly one run-identifying
    column (run_timestamp), not the old run_id/created_at/updated_at/
    last_execution_date spread.

    sourcedataurl/provenance_url are the raw graph values for this entity
    (not "whichever URL was fetched") — url_used records that separately,
    since either candidate, or neither, may have actually produced the row's
    result. See ProvenanceEntity / process_entity for how these are populated."""
    if not rows:
        return
    run_timestamp = run_started_at.isoformat()
    payload = [
        {
            "provenance":          r.get("provenance", ""),
            "sourcedataurl":       r.get("sourcedataurl", ""),
            "provenance_url":      r.get("provenance_url", ""),
            "run_timestamp":       run_timestamp,
            "url_used":            r.get("url_used", ""),
            "serial_no":           r.get("serial_no"),
            "last_refresh_date":   r.get("last_refresh_date"),
            "date_found":          bool(r.get("date_found")),
            "date_method":         r.get("date_method"),
            "date_source":         r.get("date_source"),
            "source_summary":      r.get("source_summary"),
            "tier_used":           r.get("tier_used"),
            "verification_steps":  r.get("verification_steps"),
            "tiers_attempted":     r.get("tiers_attempted", ""),
            "tier_failed_reason":  r.get("tier_failed_reason"),
            "time_to_execute_sec": r.get("extraction_time_sec"),
            "log_gcs_uri":         r.get("log_gcs_uri", ""),
        }
        for r in rows
    ]
    try:
        errors = _client(billing_project).insert_rows_json(_table(billing_project), payload)
        if errors:
            print(f"  [bq_io] insert errors (showing first 3): {errors[:3]}")
        else:
            print(f"  [bq_io] ✓ {len(payload)} rows -> {TABLE_NAME}")
    except Exception as e:
        # A transient BQ failure (network blip, quota, permission hiccup) must
        # not take down the whole run — the local CSV already has this data;
        # this row just won't have made it into Freshness_results yet.
        print(f"  [bq_io] insert failed ({type(e).__name__}: {e}) — continuing, local CSV still has this data")
