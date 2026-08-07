"""
Staleness pipeline v2 — Gemini-only tier cascade over provenance_bigquery.csv.

Three-tier cascade (Tiers 1-4 of the original design — HTTP HEAD, HTML parse,
Gemini text reasoning, Playwright static render — were removed outright: log
analysis showed their real yield was negligible next to their cost, and
everything they could find, computer-use can also find):

  Tier 0 : GitHub tree URLs only (specialized_source_handlers.handle_github_tree)
           — every other domain-specific handler (humdata, NASA NCCS, NDAP,
           EPA FTP, wikidata, Census vintage-year) was found to produce wrong
           answers often enough (e.g. identical dates across clearly distinct
           datasets) that they were removed outright rather than trusted; a
           non-GitHub URL now goes straight to Tier 1 instead.
  Tier 1 : Gemini computer-use (gemini-3.6-flash) — real browser, clicks/scrolls;
           also detects a real file download mid-session
  Tier 2 : download + plain-Gemini-API file inspection — a hand-off from within
           an in-progress Tier 1 session when it triggers an actual file
           download instead of a rendered page (see computer_use_extractor.py)

Every URL gets its own detailed log file under logs/ (every tier attempt, every
Gemini prompt/response/thought, every Tier 1 action) and every output row gets
an auto-generated, plain-English verification_steps recipe (verification_recipes.py)
so the result can be manually reproduced and checked.

Run:
  python3 staleness_pipeline_v2.py --limit 5      # smoke test
  python3 staleness_pipeline_v2.py                 # full run
"""

import argparse
import asyncio
import csv
import os
import random
import re
import time
from datetime import datetime, timezone

import aiohttp
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))


def _resolve_gemini_api_key() -> None:
    """Multi-project quota fan-out: Gemini API rate limits are enforced per
    GCP PROJECT, not per API key — running N Cloud Run Job tasks all under
    one project's key would just mean N tasks contending for the same quota
    pool (confirmed the hard way: this is exactly what caused the 429/503
    storm on a single-project run). If this is running as one task of a
    Cloud Run Job with --tasks N (Cloud Run sets CLOUD_RUN_TASK_INDEX
    automatically), and a same-indexed GEMINI_API_KEY_<index> secret is
    configured for a SEPARATE project's key, use that instead of the shared
    GEMINI_API_KEY — each task then draws from its own independent quota
    pool. Falls back to plain GEMINI_API_KEY untouched for local runs and
    single-task jobs, where this env var simply won't be set."""
    task_index = os.environ.get("CLOUD_RUN_TASK_INDEX")
    if task_index is None:
        return
    per_task_key = os.environ.get(f"GEMINI_API_KEY_{task_index}")
    if per_task_key:
        os.environ["GEMINI_API_KEY"] = per_task_key


_resolve_gemini_api_key()   # must run before computer_use_extractor/file_date_extractor
                            # are imported below — both read GEMINI_API_KEY at import time

from provenance_refresh_extractor import _parse_date, classify_url
from specialized_source_handlers import (
    _GITHUB_TREE_RE, handle_github_tree, normalize_url, classify_blocker,
)

from computer_use_extractor import tier1_computer_use
import verification_recipes as vr
import bq_io
import gcs_io

LOGS_DIR    = os.path.join(os.path.dirname(__file__), "logs")

# Env-var overridable so Cloud Run (8Gi/4vCPU, see deploy.sh) and a local
# 32GB/8-core box can safely use different values from the same image/code —
# 6 concurrent Chromium+Gemini sessions is fine locally but risks OOM on
# Cloud Run's smaller allocation, so deploy.sh pins a lower TIER1_CONCURRENCY there.
TIER0_CONCURRENCY = int(os.environ.get("TIER0_CONCURRENCY", "15"))   # cheap async HTTP — tier 0 handlers
TIER1_CONCURRENCY = int(os.environ.get("TIER1_CONCURRENCY", "6"))    # real browser + computer-use, shared
                                                                       # with tier 2's pi-agent hand-off (which
                                                                       # only fires for a minority of URLs) —
                                                                       # the expensive resource


def _noop_log(_msg: str) -> None:
    pass


def _safe_filename(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)[:150] or "unnamed"


def _write_log(serial: int, entity_id: str, urls_tried: list, log_lines: list, result: dict) -> str:
    """Writes the local .log file (useful for local runs) and returns the
    same full text so it can be uploaded to GCS (see gcs_io.upload_log) —
    Cloud Run's local filesystem disappears when the job exits, so that GCS
    copy is the only durable copy of this detail in that environment.

    One log per entity (not per URL, since a URL can now be reached via
    different entities in different roles — one's primary, another's
    fallback). urls_tried has one entry normally, two when the sourceDataUrl
    attempt failed and provenance_url was tried as a fallback; log_lines
    already has both attempts concatenated in that case (see process_entity).

    The filename is prefixed with the same serial number written to the CSV's
    serial_no column, so a row can be matched to its log file by eye without
    comparing the (long, hashed) entity_id strings."""
    full_text = (
        f"Serial: {serial}\nentity_id: {entity_id}\nurls_tried: {urls_tried}\n\n"
        + "\n\n".join(log_lines)
        + f"\n\n=== RESULT ===\n"
        f"tier_used: {result.get('tier')}\n"
        f"date: {result.get('date')}\n"
        f"source: {result.get('source')}\n"
        f"url_used: {result.get('url_used')}\n"
        f"tiers_attempted: {result.get('tiers_attempted')}\n"
        f"verification_steps: {result.get('verification_steps')}\n"
    )
    os.makedirs(LOGS_DIR, exist_ok=True)
    path = os.path.join(LOGS_DIR, f"{serial:04d}_{_safe_filename(entity_id)}.log")
    with open(path, "w", encoding="utf-8") as f:
        f.write(full_text)
    return full_text


async def process_url(session: aiohttp.ClientSession, url: str,
                       global_sem: asyncio.Semaphore,
                       tier1_sem: asyncio.Semaphore) -> tuple[dict, list[str]]:
    """Runs the Tier 0/1/2 cascade against a single URL. Returns
    (result, log_lines) rather than writing its own log — a URL can now be
    reached via different entities in different roles (one's primary
    sourceDataUrl attempt, another's provenance_url fallback), so there's no
    longer a single natural set of dataset_ids to log against here; the
    caller (process_entity) owns writing the log, once per entity."""
    log_lines = []
    def log_fn(msg: str) -> None:
        log_lines.append(msg)

    t0 = time.time()
    tried = []
    result = {"url": url, "date": None, "source": None, "tier": None,
              "error": None, "verification_steps": None}

    def _finish():
        result["tiers_attempted"] = ",".join(str(x) for x in tried)
        result["extraction_time_sec"] = round(time.time() - t0, 2)
        if not result["date"] and tried:
            result["error"] = f"no date after tiers {result['tiers_attempted']}"
        if not result["verification_steps"]:
            result["verification_steps"] = vr.recipe_no_date(result["tiers_attempted"] or "none")
        return result

    if classify_url(url) == "catalog":
        log_fn("Classified as catalog/browse URL — no single dataset date exists, skipped")
        result["error"] = "catalog/browse URL — no single dataset date exists"
        result["verification_steps"] = "N/A — catalog/browse URL, no single dataset date exists"
        return _finish(), log_lines

    if classify_blocker(url) == "not_trackable":
        log_fn("Classified as not externally trackable, skipped")
        result["error"] = "no external source — not programmatically trackable"
        result["verification_steps"] = "N/A — not externally trackable, no external source to check"
        return _finish(), log_lines

    async with global_sem:
        # Tier 0 — GitHub tree URLs only. Every other domain-specific handler
        # (humdata, NASA NCCS, NDAP, EPA FTP, wikidata, Census vintage-year)
        # was found to produce wrong answers too often — e.g. the humdata
        # handler returned the exact same date for clearly distinct
        # per-country datasets, a strong tell it was reading a platform-level
        # timestamp rather than a per-dataset one — so they were removed
        # outright rather than trusted; only the GitHub commits-API handler
        # (ground-truth-verified correct) stays.
        if _GITHUB_TREE_RE.search(url):
            log_fn(f"=== TIER 0: matched handler handle_github_tree ===")
            tried.append(0)
            r0 = await handle_github_tree(url, session)
            if r0 and r0.get("date"):
                log_fn(f"Handler found date={r0.get('date')}")
                result.update(r0)
                result["verification_steps"] = vr.recipe_tier0("handle_github_tree", url, result["date"])
                return _finish(), log_lines
            log_fn("Handler matched but found nothing, falling through to tier 1")

        # Tier 1 — Gemini computer-use (real browser). May internally hand off
        # to Tier 2 (download + plain-Gemini-API file inspection) if it detects a real file
        # download mid-session — see computer_use_extractor.tier1_computer_use.
        tried.append(1)
        r1 = await tier1_computer_use(url, tier1_sem, log_fn)
        if r1.get("date"):
            if r1.get("tier") == 2:
                tried.append(2)
                result.update({"date": r1["date"], "source": r1["source"], "tier": 2})
                result["verification_steps"] = vr.recipe_tier2_download(
                    url, r1.get("action_trace", []), r1.get("downloaded_file", ""),
                    r1["date"], r1.get("column_used", ""))
            else:
                result.update({"date": r1["date"], "source": r1["source"], "tier": 1})
                result["verification_steps"] = vr.recipe_tier1_computer_use(
                    url, r1.get("action_trace", []), r1["source"], r1["date"])
            return _finish(), log_lines
        log_fn(f"Tier 1 error: {r1.get('error')}")
        return _finish(), log_lines


async def process_entity(session: aiohttp.ClientSession, entity, serial: int,
                          global_sem: asyncio.Semaphore, tier1_sem: asyncio.Semaphore,
                          url_cache: dict, url_cache_lock: asyncio.Lock) -> dict:
    """Tries entity.sourcedataurl_candidates[0] through the Tier 0/1/2
    cascade first. Only if that finds NO date at all does it retry against
    entity.provenance_url_candidates[0] as a fallback — provenance_url is
    never attempted when sourcedataurl already succeeded (no wasted Tier
    1/2 cost). Writes exactly one log file per entity, concatenating both
    attempts' log lines when a fallback actually occurs.

    url_cache/url_cache_lock are shared across the whole run (see main()) so
    that entities whose sourcedataurl (or provenance_url) coincides with
    another entity's already-fetched URL get that result for free instead of
    re-running the cascade — the same in-flight-task pattern avoids two
    concurrent entities racing to double-fetch the same URL."""

    async def get_or_fetch(url: str) -> tuple[dict, list[str]]:
        async with url_cache_lock:
            if url not in url_cache:
                url_cache[url] = asyncio.create_task(
                    process_url(session, url, global_sem, tier1_sem))
        return await url_cache[url]

    sourcedataurl = entity.sourcedataurl_candidates[0] if entity.sourcedataurl_candidates else ""
    provenance_url = entity.provenance_url_candidates[0] if entity.provenance_url_candidates else ""

    def _finalize(result: dict, url_used: str, log_lines: list, urls_tried: list,
                  verification_steps: str | None = None) -> dict:
        final = dict(result)
        final["object_id"] = entity.entity_id
        final["sourcedataurl"] = sourcedataurl
        final["provenance_url"] = provenance_url
        final["url_used"] = url_used
        final["serial"] = serial
        if verification_steps is not None:
            final["verification_steps"] = verification_steps
        final["detailed_log"] = _write_log(serial, entity.entity_id, urls_tried, log_lines, final)
        return final

    if not sourcedataurl and not provenance_url:
        empty = {"url": "", "date": None, "source": None, "tier": None,
                 "tiers_attempted": "", "extraction_time_sec": 0.0}
        return _finalize(empty, "", [], [],
                          verification_steps="N/A — no sourceDataUrl or provenance_url candidate available")

    result_a, log_lines_a = (None, [])
    if sourcedataurl:
        result_a, log_lines_a = await get_or_fetch(sourcedataurl)
        if result_a.get("date"):
            return _finalize(result_a, sourcedataurl, log_lines_a, [sourcedataurl])

    has_distinct_fallback = bool(provenance_url) and provenance_url != sourcedataurl
    if not has_distinct_fallback:
        note = ("(provenance_url is identical to sourceDataUrl — no distinct fallback attempted)"
                if provenance_url else "(no provenance_url candidate available for fallback)")
        base_result = result_a or {"url": sourcedataurl, "date": None, "source": None, "tier": None,
                                    "tiers_attempted": "", "extraction_time_sec": 0.0}
        vsteps = (base_result.get("verification_steps") or "") + "\n\n" + note
        return _finalize(base_result, sourcedataurl, log_lines_a, [sourcedataurl] if sourcedataurl else [],
                          verification_steps=vsteps)

    # sourcedataurl found nothing (or didn't exist) and a distinct provenance_url
    # candidate exists — try it as the fallback.
    result_b, log_lines_b = await get_or_fetch(provenance_url)
    combined_lines = (
        log_lines_a + ["=== FALLBACK: sourceDataUrl found nothing, trying provenance_url ==="] + log_lines_b
        if sourcedataurl else log_lines_b
    )
    urls_tried = [sourcedataurl, provenance_url] if sourcedataurl else [provenance_url]

    if result_b.get("date"):
        vsteps = (
            vr.recipe_fallback_url(sourcedataurl, result_a.get("tiers_attempted", ""),
                                    provenance_url, result_b.get("tiers_attempted", ""),
                                    result_b.get("verification_steps", ""))
            if sourcedataurl else result_b.get("verification_steps")
        )
        return _finalize(result_b, provenance_url, combined_lines, urls_tried, verification_steps=vsteps)

    # Both attempts found nothing.
    vsteps = (
        vr.recipe_no_date_fallback(sourcedataurl, result_a.get("tiers_attempted", ""),
                                    provenance_url, result_b.get("tiers_attempted", ""))
        if sourcedataurl else result_b.get("verification_steps")
    )
    return _finalize(result_b, sourcedataurl or provenance_url, combined_lines, urls_tried, verification_steps=vsteps)


def load_urls(csv_path: str) -> list["bq_io.ProvenanceEntity"]:
    """provenance_bigquery.csv schema: predicate,object_id,value — a one-time
    export of the OLD staging-dataset, sourceDataUrl-only query (no
    provenance_url column exists in this fixture). Wrapped into the same
    ProvenanceEntity shape the BQ path uses so main()'s loop stays uniform;
    provenance_url_candidates is always empty here, so CSV-sourced entities
    never take the fallback branch in process_entity()."""
    entities: list[bq_io.ProvenanceEntity] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("predicate") != "sourceDataUrl":
                continue
            raw_url = (row.get("value") or "").strip().strip('"')
            oid = (row.get("object_id") or "").strip()
            if not raw_url or not oid:
                continue
            candidates = [u for u in normalize_url(raw_url) if u.startswith("http")]
            if not candidates:
                continue
            entities.append(bq_io.ProvenanceEntity(
                entity_id=oid, sourcedataurl_candidates=candidates, provenance_url_candidates=[]))
    return entities


# detailed_log is intentionally not a CSV column (would bloat the file;
# local logs/<serial>_<object_id>.log already has it, matched by serial_no)
# — extrasaction="ignore" lets the same row dict feed both the CSV writer
# and (in bq mode) bq_io.write_results.
CSV_FIELDNAMES = ["run_timestamp", "serial_no", "object_id", "sourcedataurl", "provenance_url", "url_used",
                  "last_refresh_date", "date_method", "date_source", "tier_used", "date_found",
                  "verification_steps", "tiers_attempted", "tier_failed_reason", "extraction_time_sec"]


def _load_completed_entities(output_csv: str) -> tuple[set[str], int]:
    """For --resume: reads an existing output CSV from a prior (possibly
    killed) run and returns (entity ids already processed, highest serial_no
    used) so a re-run skips finished work instead of redoing it, and new
    serials don't collide with existing log filenames. Keyed by object_id
    (the entity id) rather than url — a URL alone no longer uniquely
    identifies "already done" since two different entities can share one."""
    if not os.path.exists(output_csv):
        return set(), 0
    completed, max_serial = set(), 0
    with open(output_csv, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            completed.add(row.get("object_id", ""))
            try:
                max_serial = max(max_serial, int(row.get("serial_no") or 0))
            except ValueError:
                pass
    return completed, max_serial


async def main(input_csv: str | None, output_csv: str, limit: int | None,
               source: str, billing_project: str, random_sample: bool = False,
               write_bq: bool = True, resume: bool = False,
               entity_id_filter: list[str] | None = None):
    run_id = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    if source == "bq":
        if write_bq:
            bq_io.ensure_table(billing_project)   # only needed if we're about to write to it
        entities = bq_io.load_urls_from_bq(billing_project)
    else:
        if not input_csv:
            raise SystemExit("--source csv requires --input <path>")
        entities = load_urls(input_csv)

    if entity_id_filter:
        wanted = set(entity_id_filter)
        entities = [e for e in entities if e.entity_id in wanted]
        print(f"--entity-id-filter: {len(entities)} matching entities")

    if limit:
        entities = random.sample(entities, min(limit, len(entities))) if random_sample else entities[:limit]

    start_serial = 1
    append_mode = False
    if resume:
        completed_entities, max_serial = _load_completed_entities(output_csv)
        before = len(entities)
        entities = [e for e in entities if e.entity_id not in completed_entities]
        start_serial = max_serial + 1
        append_mode = bool(completed_entities)
        print(f"--resume: {before - len(entities)} entities already in {output_csv}, {len(entities)} remaining")

    unique_urls = {u for e in entities for u in (e.sourcedataurl_candidates + e.provenance_url_candidates)}
    print(f"Loaded {len(entities)} entities ({len(unique_urls)} unique candidate URLs across "
          f"sourcedataurl+provenance_url); running {len(entities)}")

    # Multi-task fan-out: when running as one task of a Cloud Run Job with
    # --tasks N (Cloud Run sets CLOUD_RUN_TASK_INDEX/CLOUD_RUN_TASK_COUNT
    # automatically in every task's container), each task only processes its
    # own 1/N slice — paired with _resolve_gemini_api_key() giving each task
    # its own separate project's Gemini quota, N tasks then run without ever
    # contending on one shared quota pool (confirmed the hard way: shared
    # quota + concurrency is exactly what caused the earlier 429/503 storm).
    # Serial numbers are assigned against the FULL list before slicing, so
    # they stay globally unique/meaningful across all tasks instead of every
    # task restarting its own numbering at 1.
    indexed_entities = list(enumerate(entities, start=start_serial))
    task_count = int(os.environ.get("CLOUD_RUN_TASK_COUNT", "1"))
    task_index = int(os.environ.get("CLOUD_RUN_TASK_INDEX", "0"))
    if task_count > 1:
        indexed_entities = indexed_entities[task_index::task_count]
        print(f"Task {task_index}/{task_count}: processing {len(indexed_entities)} of {len(entities)} entities")

    global_sem = asyncio.Semaphore(TIER0_CONCURRENCY)
    tier1_sem  = asyncio.Semaphore(TIER1_CONCURRENCY)
    connector = aiohttp.TCPConnector(ssl=False, limit=TIER0_CONCURRENCY)

    # Shared across the whole run: dedupes actual fetch/cascade work whenever
    # two entities' candidate URLs coincide (e.g. Census tid= sharing), and
    # whenever an entity's sourcedataurl equals its own provenance_url.
    url_cache: dict[str, asyncio.Task] = {}
    url_cache_lock = asyncio.Lock()

    # Written per-entity as each one finishes (not batched to the very end) —
    # so a killed process (a Chromebook going to sleep, a dropped SSH
    # session) leaves a valid, resumable CSV instead of losing everything.
    csv_file = open(output_csv, "a" if append_mode else "w", newline="", encoding="utf-8")
    csv_writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDNAMES, extrasaction="ignore")
    if not append_mode:
        csv_writer.writeheader()
        csv_file.flush()

    found = 0
    total_rows = 0
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [process_entity(session, entity, serial, global_sem, tier1_sem,
                                 url_cache, url_cache_lock)
                 for serial, entity in indexed_entities]
        done = 0
        for coro in asyncio.as_completed(tasks):
            r = await coro
            done += 1
            tier_label = f"T{r['tier']}" if r.get("tier") is not None else "miss"
            shown_url = r.get("url_used") or r.get("url") or ""
            print(f"  [{done}/{len(indexed_entities)}] {tier_label}  {r['date'] or '—':<12}  "
                  f"{r.get('object_id', '')[:40]}  {shown_url[:60]}")

            log_gcs_uri = ""
            if source == "bq" and write_bq:
                try:
                    log_gcs_uri = gcs_io.upload_log(
                        billing_project, r.get("object_id", ""), r["serial"], r.get("detailed_log", ""))
                except Exception as e:
                    print(f"  [gcs_io] log upload failed for {r.get('object_id', '')[:40]} "
                          f"({type(e).__name__}: {e}) — continuing without it (local logs/ still has the full log)")

            row = {
                "run_timestamp":       run_id,
                "serial_no":           r["serial"],
                "object_id":           r.get("object_id", ""),
                "sourcedataurl":       r.get("sourcedataurl", ""),
                "provenance_url":      r.get("provenance_url", ""),
                "url_used":            r.get("url_used", ""),
                "last_refresh_date":   r["date"],
                "date_method":         vr.method_label(r["tier"], r["source"]),
                "date_source":         r["source"],
                "tier_used":           r["tier"],
                "date_found":          bool(r["date"]),
                "verification_steps":  r.get("verification_steps"),
                "tiers_attempted":     r.get("tiers_attempted", ""),
                "tier_failed_reason":  r.get("error"),
                "extraction_time_sec": r.get("extraction_time_sec"),
                "log_gcs_uri":         log_gcs_uri,
            }
            csv_writer.writerow(row)
            total_rows += 1
            if row["date_found"]:
                found += 1
            csv_file.flush()   # survive a kill/sleep between completions, not just at the end
            if source == "bq" and write_bq:
                bq_io.write_results(billing_project, run_id, [row])

    csv_file.close()
    print(f"\nResults saved -> {output_csv}")
    print(f"Found date: {found}/{max(total_rows, 1)} ({found * 100 // max(total_rows, 1)}%)")
    if source == "bq" and not write_bq:
        print("(--no-bq-write set — skipped writing to data_freshness_report, results are only in the local CSV)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["csv", "bq"], default=os.environ.get("PIPELINE_SOURCE", "bq"),
                    help="where to read the URL list from (default: bq)")
    ap.add_argument("--input",  default=None, help="required when --source csv")
    ap.add_argument("--output", default=os.path.join(os.path.dirname(__file__), "staleness_results.csv"))
    ap.add_argument("--billing-project", default=os.environ.get("GCP_PROJECT", "datcom-import-dev-768877"),
                    help="GCP project used to run the BQ query job and to hold data_freshness_report + the log bucket")
    ap.add_argument("--limit",  type=int, default=None, help="cap number of URLs (for testing)")
    ap.add_argument("--random", action="store_true",
                    help="with --limit N, pick N random URLs instead of the first N (e.g. for a representative smoke test)")
    ap.add_argument("--no-bq-write", action="store_true",
                    help="with --source bq: still fetch input from BigQuery, but only write the local CSV — skip data_freshness_report and the GCS log upload entirely")
    ap.add_argument("--entity-id-filter", default=None,
                    help="comma-separated entity ids to run instead of the full/limited set — for targeting "
                         "specific known fallback-trigger entities during testing (see README's testing plan)")
    args = ap.parse_args()
    entity_id_filter = (
        [e.strip() for e in args.entity_id_filter.split(",") if e.strip()]
        if args.entity_id_filter else None
    )
    asyncio.run(main(args.input, args.output, args.limit, args.source, args.billing_project,
                      args.random, not args.no_bq_write, entity_id_filter=entity_id_filter))
