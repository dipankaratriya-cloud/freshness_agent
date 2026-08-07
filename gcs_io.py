"""
GCS log storage for staleness_pipeline_v2 — each per-URL log gets uploaded
as its own file instead of embedded inline in BigQuery (unlike the old
refresh_dates_v2's detailed_log STRING column). The BigQuery row stores just
the gs:// URI pointing at it.

Logs live under gs://BUCKET/<run folder>/<serial>_<provenance>.log — one
folder per pipeline run (see run_folder_name()) rather than flat in the
bucket root, so browsing the bucket shows one entry per run instead of every
entity from every run ever mixed together.
"""

import os
import re
from datetime import datetime

from google.cloud import storage

BUCKET = os.environ.get("GCS_BUCKET", "freshness_logs")
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


def run_folder_name(run_started_at: datetime) -> str:
    """One pipeline run = one folder, named after when it started.

    A single run can be several Cloud Run Job TASKS (see --tasks N in
    cloudbuild.yaml + _resolve_gemini_api_key()'s per-task Gemini key), each
    its own container computing run_started_at independently — their clocks
    can be a few seconds apart, so a full HH-MM-SS timestamp would give each
    task its own folder instead of one shared folder for the whole run.
    CLOUD_RUN_EXECUTION is what Cloud Run actually guarantees is identical
    across every task of the same job execution, so when it's set (i.e. this
    is a Cloud Run Job, not a local run) that's what makes the folder name
    agree; the date prefix is kept only for human readability when browsing
    the bucket, not for uniqueness."""
    execution = os.environ.get("CLOUD_RUN_EXECUTION")
    if execution:
        return f"{run_started_at.strftime('%Y-%m-%d')}_{execution}"
    return run_started_at.strftime("%Y-%m-%d_%H-%M-%S")


def upload_log(billing_project: str, run_folder: str, provenance: str, serial: int, log_text: str) -> str:
    """Uploads one per-URL log to gs://BUCKET/<run_folder>/<serial>_<provenance_safe>.log —
    same filename convention as the local logs/ directory, just nested under
    this run's folder — and returns the gs:// URI to store in the BigQuery
    row's log_gcs_uri column."""
    blob_name = f"{run_folder}/{serial:04d}_{_safe_filename(provenance)}.log"
    bucket = _client(billing_project).bucket(BUCKET)
    blob = bucket.blob(blob_name)
    blob.upload_from_string(log_text, content_type="text/plain")
    return f"gs://{BUCKET}/{blob_name}"
