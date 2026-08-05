#!/bin/bash
# Deploy the Gemini-only staleness pipeline as its own Cloud Run Job.
# Usage: bash deploy.sh
#
# This just calls cloudbuild.yaml via Cloud Build, which now does build +
# push + deploy in one pipeline (see that file for the actual deploy step) —
# no local Docker required, so this works from a machine with only `gcloud`
# installed.
set -euo pipefail

cd "$(dirname "$0")"

PROJECT="datcom-import-dev-768877"
REGION="us-central1"
BQ_DATASET="dc_kg_dashboard"
JOB="staleness-pipeline-v2"

echo "▶ Setting project"
gcloud config set project "$PROJECT"

# ── Build + push + deploy (all handled by cloudbuild.yaml's 4 steps) ─────────
echo "▶ Building, pushing, and deploying via Cloud Build"
gcloud builds submit --config cloudbuild.yaml .

echo ""
echo "✅ Deploy complete"
echo "   Manual trigger : gcloud run jobs execute ${JOB} --region=${REGION}"
echo "   Run logs       : gcloud run jobs executions list --job=${JOB} --region=${REGION}"
echo "   BQ results     : bq query 'SELECT * FROM \`${PROJECT}.${BQ_DATASET}.data_freshness_report\` ORDER BY last_execution_date DESC LIMIT 50'"
echo ""
echo "⚠  BEFORE the first real run: whichever identity/service account this job"
echo "   runs as needs read access on datcom-store — the SOURCE_QUERY in"
echo "   bq_io.py reads datcom-store.dc_graph_staging_2026_07_28.{Edge,Node},"
echo "   a project owned by the data-engineering team. Grant it with, e.g.:"
echo "     gcloud projects add-iam-policy-binding datcom-store \\"
echo "       --member=\"serviceAccount:<job-runtime-service-account>\" --role=roles/bigquery.dataViewer"
echo "   Confirmed this session: a 403 Access Denied on datcom-store is exactly"
echo "   what happens without this grant — it's a real prerequisite, not hypothetical."
echo ""
echo "   Note: this script does not set --service-account, so the job runs as"
echo "   the project's default Compute Engine service account unless you add"
echo "   one to cloudbuild.yaml's deploy step — add it there if you have a"
echo "   dedicated runtime service account for this pipeline."
