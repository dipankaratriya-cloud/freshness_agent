#!/bin/bash
# Deploy the Gemini-only staleness pipeline as its own Cloud Run Job.
# Usage: bash deploy.sh
set -euo pipefail

cd "$(dirname "$0")"

PROJECT="datcom-infosys-dev"
REGION="us-central1"
BQ_DATASET="staleness"
IMAGE="gcr.io/${PROJECT}/staleness-pipeline-v2:latest"
SA="staleness-runner@${PROJECT}.iam.gserviceaccount.com"   # reused from the existing pipeline
JOB="staleness-pipeline-v2"

echo "▶ Setting project"
gcloud config set project "$PROJECT"

echo "▶ Building Docker image"
gcloud builds submit --tag "$IMAGE" .

# refresh_dates_v2 is created/schema-migrated automatically at run time by
# bq_io.ensure_table() — nothing to pre-create here.
echo "▶ Deploying Cloud Run Job"
gcloud run jobs create "$JOB" \
  --image "$IMAGE" \
  --region "$REGION" \
  --service-account "$SA" \
  --memory 8Gi \
  --cpu 4 \
  --task-timeout 86400 \
  --max-retries 1 \
  --set-env-vars "GCP_PROJECT=${PROJECT},BQ_DATASET=${BQ_DATASET},PIPELINE_SOURCE=bq" \
  --set-secrets "GEMINI_API_KEY=staleness-gemini-api-key:latest" \
  2>/dev/null || \
gcloud run jobs update "$JOB" \
  --image "$IMAGE" \
  --region "$REGION" \
  --memory 8Gi \
  --cpu 4 \
  --set-env-vars "GCP_PROJECT=${PROJECT},BQ_DATASET=${BQ_DATASET},PIPELINE_SOURCE=bq" \
  --set-secrets "GEMINI_API_KEY=staleness-gemini-api-key:latest"

echo ""
echo "✅ Deploy complete"
echo "   Manual trigger : gcloud run jobs execute ${JOB} --region=${REGION}"
echo "   Run logs       : gcloud run jobs executions list --job=${JOB} --region=${REGION}"
echo "   BQ results     : bq query 'SELECT * FROM \`${PROJECT}.${BQ_DATASET}.refresh_dates_v2\` ORDER BY run_date DESC LIMIT 50'"
echo ""
echo "⚠  BEFORE the first real run: ${SA} needs read access on datcom-store"
echo "   (the SOURCE_QUERY in bq_io.py reads datcom-store.dc_graph_staging_2026_07_28.{Edge,Node})."
echo "   This grant has to come from whoever administers the datcom-store project — e.g.:"
echo "     gcloud projects add-iam-policy-binding datcom-store \\"
echo "       --member=\"serviceAccount:${SA}\" --role=roles/bigquery.dataViewer"
echo "   Without it, the job will fail with a 403 Access Denied."
