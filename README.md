# Staleness Pipeline v2 — Gemini-only tier cascade

A three-tier pipeline that determines the last-refresh date of provenance/
dataset source URLs: domain-specific direct handlers, Gemini computer-use
(real browser), and — new — a download+pi-coding-agent hand-off for URLs
that trigger a real file download instead of rendering a page. Runs either
against a local CSV or, by default, against a live BigQuery query — and can
run either locally or as its own Cloud Run Job.

## Input

`--source bq` (default): runs `bq_io.SOURCE_QUERY` — an `Edge`/`Node` join in
`datcom-store.spanner_dc_graph_prod_DEFAULT` (the prod graph), fetching
**both** the `sourceDataUrl` edge's own value and its sibling `url` edge's
value (`provenance_url`) for each provenance entity — fresh on every run, so
the input always reflects current provenance data rather than a frozen
export. Returns a list of
`bq_io.ProvenanceEntity(entity_id, sourcedataurl_candidates, provenance_url_candidates)`.

**Fallback cascade**: for each entity, the full Tier 0/1/2 cascade runs
against `sourcedataurl` first. Only if that finds no date at all is
`provenance_url` tried as a second attempt (`process_entity()` in
`staleness_pipeline_v2.py`). `provenance_url` is skipped entirely when it's
empty or identical to `sourcedataurl` — no wasted second attempt. A shared,
run-wide URL-level task cache means two entities whose candidate URLs happen
to coincide (e.g. a shared Census `tid=` URL) only pay for the cascade once.

`--source csv --input <path>`: reads a local CSV with the
`predicate,object_id,value` shape (e.g. `provenance_bigquery.csv`, a one-time
export of the sourceDataUrl-only query) — useful for local development
without BigQuery credentials. This fixture has no `provenance_url` column, so
CSV-sourced entities never take the fallback branch.

`--entity-id-filter id1,id2,...`: run only the named entities instead of the
full/limited set — for targeting specific known fallback-trigger entities
during testing (see "Testing the fallback cascade" below).

## Output

**BigQuery** (`--source bq`, the default): one row per entity in
`<project>.dc_kg_dashboard.data_freshness_report` (auto-created/schema-migrated
on startup by `bq_io.ensure_table()`) — a shared dashboard table, append-only,
one row per entity per run. `object_id` is the entity's own true id
(`e1.subject_id`). `sourcedataurl`/`provenance_url` are the two raw candidate
values; `url_used` records whichever one actually produced (or was used to
attempt) the row's result — see the CSV column list below for the rest.

**`staleness_results.csv`** — written in both modes (small/cheap, useful for
local inspection either way): `run_timestamp, serial_no, object_id,
sourcedataurl, provenance_url, url_used, last_refresh_date, date_method,
date_source, tier_used, date_found, verification_steps, tiers_attempted,
tier_failed_reason, extraction_time_sec`. `run_timestamp` is the same
`YYYY-MM-DD HH:MM:SS UTC` value across every row of one run — it's the same
string passed as `run_id` to the BQ side, so a local CSV and its matching BQ
rows can be cross-referenced by this value.

**`logs/<serial>_<object_id>.log`** — one detailed log file per **entity**
(not per URL — a URL can be reached via different entities in different
roles), same content as the `detailed_log` BQ column: every tier attempted
for every URL tried (both attempts, concatenated, when a fallback occurred),
every Gemini prompt/response/reasoning ("thought") trace, and for Tier 1
every browser action taken with its stated intent (plus, when a Tier 2
hand-off occurs, the pi agent's own reasoning trace). On `--source bq`
(unless `--no-bq-write` is set), each log is also uploaded to
`gs://datcom-import-dev-768877-staleness-logs/<serial>_<object_id>.log` (see
`gcs_io.py`) — that's the durable copy on Cloud Run, where the local
filesystem disappears once the job exits; the BQ row's `log_gcs_uri` column
points at it.

## Tier cascade

| Tier | Method | Notes |
|---|---|---|
| 0 | `specialized_source_handlers.handle_github_tree` (GitHub tree URLs only) | direct GitHub commits-API call, no LLM. Every other domain-specific handler (humdata, NASA NCCS, NDAP, EPA FTP, wikidata, Census vintage-year) was removed after log analysis showed they produced wrong answers too often (e.g. the humdata handler returning the exact same date for clearly distinct per-country datasets) — a non-GitHub URL now skips Tier 0 entirely and goes straight to Tier 1 |
| 1 | **Gemini computer-use** (`gemini-3.6-flash`) | real headless browser: screenshots, clicks, scrolls, navigates, up to 40 actions; also detects a real file download starting mid-session |
| 2 | Download + **plain Gemini API** file inspection | not an independent fallback — a hand-off from *within* a live Tier 1 session when it triggers an actual file download instead of a rendered page; the file is saved, the browser session ends, and `file_date_extractor.extract_date_from_file()` inspects it via two plain Gemini API calls (no agent, no subprocess) — see below |

An earlier design had additional Tiers 1-4 (HTTP HEAD, HTML parse, Gemini
text reasoning, Playwright static render) and a Tier 5 (Groq `compound-beta`
real-browsing); all were removed outright — their real yield was negligible
next to their cost, and everything they could find, Tier 1's real browsing
can also find. A URL only reaches Tier 1 if Tier 0 found nothing; Tier 2 only
ever fires as a continuation of an in-progress Tier 1 session, never on its
own. This whole cascade is what `process_url()` runs once per URL;
`process_entity()` wraps it with the sourcedataurl→provenance_url fallback
described above.

## Files

| File | Role |
|---|---|
| `staleness_pipeline_v2.py` | main orchestrator — `process_url()` runs the Tier 0/1/2 cascade on one URL, `process_entity()` wraps it with the sourcedataurl→provenance_url fallback and owns per-entity logging, `main()` loads entities and drives the run |
| `provenance_refresh_extractor.py` | Tier 0's date-parsing helpers (`_parse_date`, `classify_url`), plus its own Groq-based Tier 3/5 functions, which exist in this file but are unused here |
| `specialized_source_handlers.py` | Tier 0's GitHub commits-API handler (`handle_github_tree`) — the only handler this pipeline still uses; the file's other domain handlers exist but are no longer imported here |
| `computer_use_prompt.py` | prompt text for the Tier 1 Gemini computer-use tier |
| `computer_use_extractor.py` | Tier 1 implementation (`tier1_computer_use`) — Playwright action execution, safety-decision handling, action-trace logging, and the Tier 2 download hand-off (`_save_download`/`_inspect_downloaded_file`); also runnable standalone |
| `file_date_extractor.py` | Tier 2's file-inspection logic — two Gemini calls, no coding agent, no subprocess: Step A picks which column represents the observation period from a small file preview; Step B reads every distinct value in that column across the whole file (no row-count cap) and asks Gemini 3.1 Pro to identify the max from that complete list — handles real-world messiness (mixed types, inconsistent formats) that hand-written parsing code kept breaking on, confirmed against a real 80k-row UN data export. Replaces an earlier pi-coding-agent-CLI-based version, removed after that tool was found to violate policy for use on Google source/data |
| `verification_recipes.py` | builds the auto-generated, plain-English `verification_steps` recipe for every row, one function per tier, plus `recipe_fallback_url()`/`recipe_no_date_fallback()` for the two-attempt case |
| `bq_io.py` | BigQuery I/O — the live source query (`ProvenanceEntity` records), `data_freshness_report` table creation, and writing results |
| `gcs_io.py` | uploads each entity's detailed log to `gs://datcom-import-dev-768877-staleness-logs/` and returns the `gs://` URI stored in the BQ row's `log_gcs_uri` column |
| `Dockerfile` | Cloud Run Job image — plain Python/Playwright deps only; no agent CLI, no Node, nothing beyond `pip install` |
| `cloudbuild.yaml` | full CI pipeline — builds, pushes to Artifact Registry, then deploys the `staleness-pipeline-v2` Cloud Run Job |
| `deploy.sh` | thin wrapper that sets the project and calls `cloudbuild.yaml` via `gcloud builds submit` |

No Groq call is ever made by this pipeline — `provenance_refresh_extractor.py`
does construct an idle, unused `Groq()` client object at import time, but
`.chat.completions.create` is never invoked from here.

## Verification recipes

Every row — including misses — gets a `verification_steps` string built
deterministically from what actually happened during the run (not written by
an LLM after the fact), e.g.:

- Tier 0: `STEP 1: matched domain-specific handler ... STEP 3: Handler returned date -> <date>.`
- Tier 1: the full numbered action sequence (`clicked X (intent: ...)`, `navigated to Y`, ...) ending in the model's cited source
- Tier 2: continues Tier 1's action sequence, then `a real file download started ... handed off to plain-Gemini-API file inspection ... identified column '<col>' -> <date>.`
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
`--billing-project` (default `datcom-import-dev-768877`). Tier 2 additionally
requires the `pi` CLI to be installed and authenticated locally (the Cloud
Run image installs and authenticates it too — see the Dockerfile row above).

**Concurrency**: `TIER0_CONCURRENCY` (default 15, env-var overridable) bounds
the cheap Tier 0 handler HTTP calls; `TIER1_CONCURRENCY` (default 6, env-var
overridable) bounds real browser+Gemini computer-use sessions and is shared
with Tier 2's file-inspection hand-off (which only fires for the minority of
URLs that trigger a real download, so one semaphore avoids either starving
the other). Both constants read `os.environ` at import time
(`TIER0_CONCURRENCY=N python3 staleness_pipeline_v2.py ...` locally, or
`--set-env-vars` on Cloud Run) so the same image/code can run a different
value in Cloud Run than locally. `cloudbuild.yaml`'s deploy step pins
`TIER1_CONCURRENCY=3` (down from the code's local-machine default of 6) —
found the hard way that Gemini's rate limits (`429 RESOURCE_EXHAUSTED`) are a
real, easy-to-hit ceiling at higher concurrency on a real production-sized
batch (see below).

**Multi-project quota fan-out**: Gemini API rate limits are enforced
**per GCP project**, not per API key — running more workers under one
project's key just means more contention for the same quota, not more real
throughput (confirmed directly: this is what caused a `429`/`503` storm on a
432-entity production run). If genuinely separate
projects/billing accounts are available, `_resolve_gemini_api_key()` lets
each Cloud Run Job task draw from its own independent quota pool instead:

- Deploy with `--tasks N --parallelism N` — Cloud Run automatically sets
  `CLOUD_RUN_TASK_INDEX` (0 to N-1) and `CLOUD_RUN_TASK_COUNT` in every task's
  container.
- `main()` shards the entity list `1/N` per task (`indexed_entities[task_index::task_count]`)
  — serial numbers are assigned against the *full* list before slicing, so
  they stay globally unique/meaningful across all tasks rather than every
  task restarting its own numbering at 1.
- `_resolve_gemini_api_key()` swaps in `GEMINI_API_KEY_<task_index>` (if set)
  in place of the shared `GEMINI_API_KEY`, *before* `computer_use_extractor`/
  `file_date_extractor` are imported (both read the key at import time).
- Falls back to the plain shared `GEMINI_API_KEY` untouched for local runs
  and single-task jobs, where `CLOUD_RUN_TASK_INDEX` simply won't be set —
  this is fully backward compatible, not a required setup.

`cloudbuild.yaml`'s deploy step is wired for 5 tasks/5 separate projects —
`GEMINI_API_KEY_0` through `GEMINI_API_KEY_4`, each mapped from its own
Secret Manager secret (placeholder names `gemini-api-key-project-0` through
`-4` — create these ahead of time, one per project's key, and rename the
mapping if you use different secret names).

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
`roles/bigquery.dataViewer` there on the **`spanner_dc_graph_prod_DEFAULT`**
dataset specifically (the grant is dataset-scoped) before
`bq_io.load_urls_from_bq()` can succeed; `deploy.sh` prints the exact command
needed at the end of its output. Confirmed this session: a `gcloud` login
with valid Application Default Credentials got `403 Access Denied` running
this query without the grant — so it's a real prerequisite, not a
hypothetical one.

**Tier 2's auth**: it's a plain Gemini API call, so it needs nothing beyond
the same `GEMINI_API_KEY` every other tier already uses — no separate
CLI/install/auth step.

## Testing the fallback cascade

Most entities where `sourcedataurl` already works will never exercise the
`provenance_url` fallback branch, so a plain `--limit`/`--random` smoke test
mostly re-tests the unchanged happy path. To actually validate the fallback:

1. Query for entities where `sourcedataurl != provenance_url AND provenance_url IS NOT NULL` to find the real candidate pool.
2. Spot-check a few of those `sourcedataurl` values directly (e.g. a plain HTTP check) to find ones that are genuinely dead/blocked — these are guaranteed to exercise the full fallback path.
3. Run those specific entities with `--entity-id-filter id1,id2,...` and `--no-bq-write` (don't write test rows into the shared table), and confirm the per-entity log shows `ATTEMPT 1 (sourceDataUrl): ... no usable date found` followed by `ATTEMPT 2 (provenance_url fallback): ...` ending in a found date.
4. Also test 1-2 entities where `sourcedataurl == provenance_url` (confirm the cache makes the second attempt free) and 1-2 with a known-good `sourcedataurl` (confirm the fallback branch is never entered at all).
