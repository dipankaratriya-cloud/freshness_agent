"""
GCS log storage for staleness_pipeline_v2 — each per-URL log gets uploaded
as its own file instead of embedded inline in BigQuery (unlike the old
refresh_dates_v2's detailed_log STRING column). The BigQuery row stores just
the gs:// URI pointing at it.
"""

import os
import re

from google.cloud import storage

BUCKET = os.environ.get("GCS_BUCKET", "datcom-import-dev-768877-staleness-logs")
_client_cache: dict[str, storage.Client] = {}


def _client(billing_project: str) -> storage.Client:
    if billing_project not in _client_cache:
        _client_cache[billing_project] = storage.Client(project=billing_project)
    return _client_cache[billing_project]


def _safe_filename(s: str) -> str:
    # Same sanitizing rule as staleness_pipeline_v2._safe_filename() — kept
    # as a local copy rather than imported, to avoid a circular import
    # (staleness_pipeline_v2 is the one importing this module).
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)[:150] or "unnamed"


def upload_log(billing_project: str, object_id: str, serial: int, log_text: str) -> str:
    """Uploads one per-URL log to gs://BUCKET/<serial>_<object_id_safe>.log —
    same filename convention as the local logs/ directory — and returns the
    gs:// URI to store in the BigQuery row's log_gcs_uri column."""
    blob_name = f"{serial:04d}_{_safe_filename(object_id)}.log"
    bucket = _client(billing_project).bucket(BUCKET)
    blob = bucket.blob(blob_name)
    blob.upload_from_string(log_text, content_type="text/plain")
    return f"gs://{BUCKET}/{blob_name}"
