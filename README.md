# Staleness Pipeline v2 — Gemini-only tier cascade

Rebuild of the original 6-tier `provenance_refresh_extractor.py` cascade with
Groq removed entirely and a new Gemini computer-use tier added. Runs either
against a local CSV or, by default, against a live BigQuery query — and can
run either locally or as its own Cloud Run Job.

## Input

`--source bq` (default): runs `bq_io.SOURCE_QUERY` — an `Edge`/`Node` join in
`datcom-store.dc_graph_staging_2026_07_28` filtered to
`predicate = 'sourceDataUrl'` — fresh on every run, so the input always
reflects current provenance data rather than a frozen export.

`--source csv --input <path>`: reads a local CSV with the same
`predicate,object_id,value` shape (e.g. `provenance_bigquery.csv`, a one-time
export of the same query) — useful for local development without BigQuery
credentials.

## Output

**BigQuery** (`--source bq`, the default): one row per dataset `object_id` in
`datcom-import-dev-768877.dc_kg_dashboard.data_freshness_report`
(auto-created/schema-migrated on startup by `bq_io.ensure_table()`) — a
shared dashboard table, append-only, one row per dataset per run. Same
columns as the CSV below, plus `log_gcs_uri` pointing at the full per-URL log
in GCS (see below) rather than embedding the log text inline.

**`staleness_results.csv`** — written in both modes (small/cheap, useful for
local inspection either way): `object_id, url, last_refresh_date, date_source,
tier_used, date_found, verification_steps, tiers_attempted,
tier_failed_reason, extraction_time_sec`.

**`logs/<serial>_<object_id>.log`** — one detailed log file per URL: every
tier attempted, every Gemini prompt/response/reasoning ("thought") trace, and
for Tier 6 every browser action taken with its stated intent. On
`--source bq` (unless `--no-bq-write` is set), each log is also uploaded to
`gs://datcom-import-dev-768877-staleness-logs/<serial>_<object_id>.log` (see
`gcs_io.py`) — that's the durable copy on Cloud Run, where the local
filesystem disappears once the job exits; the BQ row's `log_gcs_uri` column
points at it.

## Tier cascade

| Tier | Method | Notes |
|---|---|---|
| 0 | `specialized_source_handlers.SPECIALIZED_HANDLERS` | direct API/vintage-year handlers, no LLM |
| 1 | HTTP HEAD → `Last-Modified` | |
| 2 | GET + HTML (JSON-LD / meta / body-text regex, sub-link following) | |
| 3 | **Gemini 3.1 Pro** reads Tier 2's already-fetched text | replaces Groq `gpt-oss-120b`; text-only, never browses |
| 4 | Playwright full render, then either the same structured extraction as Tier 2 or Tier 3's Gemini function on the rendered text | |
| ~~5~~ | *(deleted)* | was Groq `compound-beta`, an opaque real-browsing agent; Tier 6 already does real, logged browsing so it's redundant |
| 6 | **Gemini computer-use** (`gemini-3.6-flash`) | real headless browser: screenshots, clicks, scrolls, navigates, up to 12 actions |

A URL only reaches a later tier if every earlier tier found nothing.

## Files

| File | Role |
|---|---|
| `staleness_pipeline_v2.py` | main orchestrator — loads the URL list, runs the cascade per URL, writes logs + final CSV/BigQuery output |
| `provenance_refresh_extractor.py` | Tiers 1/2's HTTP-fetch + HTML-parsing logic, and shared date-parsing/recency-guard helpers (its Groq-based Tier 3/5 functions exist but are unused here) |
| `specialized_source_handlers.py` | Tier 0's direct-API/vintage-year handlers |
| `tier3_prompt.py` | prompt text for the Gemini 3.1 Pro text-reasoning tier |
| `computer_use_prompt.py` | prompt text for the Gemini computer-use tier |
| `computer_use_extractor.py` | Tier 6 implementation (`tier6_computer_use`) — Playwright action execution, safety-decision handling, action-trace logging; also runnable standalone |
| `verification_recipes.py` | builds the auto-generated, plain-English `verification_steps` recipe for every row, one function per tier |
| `bq_io.py` | BigQuery I/O — the live source query, `data_freshness_report` table creation, and writing results |
| `gcs_io.py` | uploads each per-URL log to `gs://datcom-import-dev-768877-staleness-logs/` and returns the `gs://` URI stored in the BQ row's `log_gcs_uri` column |
| `Dockerfile` | Cloud Run Job image |
| `cloudbuild.yaml` | full CI pipeline — builds, pushes to Artifact Registry, then deploys the `staleness-pipeline-v2` Cloud Run Job |
| `deploy.sh` | thin wrapper that sets the project and calls `cloudbuild.yaml` via `gcloud builds submit` |

No Groq call is ever made by this pipeline — `provenance_refresh_extractor.py`
does construct an idle, unused `Groq()` client object at import time, but
`.chat.completions.create` is never invoked from here.

## Verification recipes

Every row — including misses — gets a `verification_steps` string built
deterministically from what actually happened during the run (not written by
an LLM after the fact), e.g.:

- Tier 1: `STEP 1: HTTP HEAD <url>. STEP 2: read Last-Modified header -> <date>.`
- Tier 2/4 (direct): names the exact JSON-LD field / meta tag / body-text match
- Tier 3/4 (Gemini fallback): `STEP 3: ask Gemini 3.1 Pro ... STEP 4: model cited '<snippet>' -> <date>.`
- Tier 6: the full numbered action sequence (`clicked X (intent: ...)`, `navigated to Y`, ...) ending in the model's cited source
- Miss: lists which tiers were attempted

## Run locally

```
pip install -r requirements.txt
playwright install chromium

python3 staleness_pipeline_v2.py --source csv --input provenance_bigquery.csv --limit 5   # smoke test, no BQ needed
python3 staleness_pipeline_v2.py                                                           # full run, --source bq by default
```

Requires `GEMINI_API_KEY` set in `.env` (copy `.env.example`) or the
environment. `--source bq` additionally requires GCP credentials with query
access to `datcom-store` (see the deploy caveat below) and write access to
`--billing-project` (default `datcom-import-dev-768877`).

## Deploy to Cloud Run

```
bash deploy.sh
```

or directly:

```
gcloud builds submit --config cloudbuild.yaml .
```

`cloudbuild.yaml` does the full build → push → deploy in one Cloud Build
pipeline (no local Docker needed, so this works from a machine with only
`gcloud` installed) — it deploys a Cloud Run Job named `staleness-pipeline-v2`
in project `datcom-import-dev-768877`. Trigger manually with `gcloud run jobs
execute staleness-pipeline-v2 --region=us-central1`; there's no recurring
schedule wired up yet.

**Before the first real run**: whichever identity/service account this job
runs as (the deploy step in `cloudbuild.yaml` doesn't set `--service-account`,
so it defaults to the project's Compute Engine default service account
unless you add one) has no grant at all on `datcom-store` — that's a
separate project owned by the data-engineering team. It needs
`roles/bigquery.dataViewer` there (at minimum, on the
`dc_graph_staging_2026_07_28` dataset) before `bq_io.load_urls_from_bq()` can
succeed; `deploy.sh` prints the exact command needed at the end of its
output. Confirmed this session: a `gcloud` login with valid Application
Default Credentials got `403 Access Denied` running the same query without
this grant — so it's a real prerequisite, not a hypothetical one.

## Known caveat

Log files are named from each row's first `object_id`. If a source row's
value is a comma-separated multi-URL field that gets split into two URLs
sharing the same `object_id`, their two log files collide (the second
overwrites the first) — harmless if both are misses, but worth knowing.
