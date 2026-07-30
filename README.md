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
`<project>.staleness.refresh_dates_v2` (auto-created/schema-migrated on
startup by `bq_io.ensure_table()`) — same columns as the CSV below, plus a
`detailed_log` column holding the *entire* per-URL log text inline. This is
the durable copy on Cloud Run, where the local filesystem disappears once the
job exits.

**`staleness_results.csv`** — written in both modes (small/cheap, useful for
local inspection either way): `object_id, url, last_refresh_date, date_source,
tier_used, date_found, verification_steps, tiers_attempted,
tier_failed_reason, extraction_time_sec`.

**`logs/<object_id>.log`** — one detailed log file per URL, same content as
the `detailed_log` BQ column: every tier attempted, every Gemini
prompt/response/reasoning ("thought") trace, and for Tier 6 every browser
action taken with its stated intent. Only persists locally — not uploaded
anywhere on Cloud Run (the BQ column is the durable copy there).

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
| `bq_io.py` | BigQuery I/O — the live source query, `refresh_dates_v2` table creation, and writing results |
| `Dockerfile` | Cloud Run Job image |
| `deploy.sh` | deploys this pipeline as a Cloud Run Job |

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
`--billing-project` (default `datcom-infosys-dev`).

## Deploy to Cloud Run

```
bash deploy.sh
```

Builds via Cloud Build (no local Docker needed, so this works from a machine
with only `gcloud` installed) and deploys a Cloud Run Job,
`staleness-pipeline-v2`. Trigger manually with `gcloud run jobs execute
staleness-pipeline-v2 --region=us-central1`; there's no recurring schedule
wired up yet.

**Before the first real run**: the job's service account
(`staleness-runner@datcom-infosys-dev.iam.gserviceaccount.com`) has no grant
at all on `datcom-store` — that's a separate project owned by the
data-engineering team. It needs `roles/bigquery.dataViewer` there (at
minimum, on the `dc_graph_staging_2026_07_28` dataset) before
`bq_io.load_urls_from_bq()` can succeed; `deploy.sh` prints the exact command
needed at the end of its output. Confirmed directly: a `gcloud` login with
valid Application Default Credentials but no grant on `datcom-store` gets
`403 Access Denied` running this query — so this grant is a real
prerequisite, not a hypothetical one.

## Known caveat

Log files are named from each row's first `object_id`. If a source row's
value is a comma-separated multi-URL field that gets split into two URLs
sharing the same `object_id`, their two log files collide (the second
overwrites the first) — harmless if both are misses, but worth knowing.
