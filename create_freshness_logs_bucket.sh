#!/usr/bin/env bash
# One-time setup: creates the GCS bucket gcs_io.py writes per-run log
# folders into (gs://freshness_logs/<run folder>/<serial>_<provenance>.log).
# Run this once, by hand, before the pipeline's first run against the new
# Freshness_results/freshness_logs setup -- gcs_io.py does NOT create the
# bucket itself (only ensure_table() auto-creates the BQ dataset/table; a
# GCS bucket create is a real, billable resource and shouldn't happen
# silently as a side effect of a pipeline run).
#
# Usage:
#   ./create_freshness_logs_bucket.sh [PROJECT] [BUCKET_NAME] [LOCATION]
# Defaults match the pipeline's existing project/region.

set -euo pipefail

PROJECT="${1:-datcom-import-dev-768877}"
BUCKET="${2:-freshness_logs}"
LOCATION="${3:-us-central1}"

echo "Creating gs://${BUCKET} in project ${PROJECT} (${LOCATION})..."
gcloud storage buckets create "gs://${BUCKET}" \
  --project="${PROJECT}" \
  --location="${LOCATION}" \
  --uniform-bucket-level-access

echo "Done. If BUCKET_NAME above isn't the literal string 'freshness_logs'," \
     "set GCS_BUCKET=${BUCKET} in the Cloud Run Job's env vars (see" \
     "cloudbuild.yaml) so gcs_io.py points at it -- its default is" \
     "'freshness_logs' and only needs overriding if you chose a different name."
