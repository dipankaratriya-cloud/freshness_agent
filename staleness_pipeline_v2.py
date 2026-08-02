"""
Staleness pipeline v2 — Gemini-only tier cascade over provenance_bigquery.csv.

Groq-free rebuild of provenance_refresh_extractor.py's Tier 0-5 cascade plus
this folder's Tier 6 computer-use fallback:

  Tier 0 : specialized_source_handlers.SPECIALIZED_HANDLERS (direct API/vintage)
  Tier 1 : HTTP HEAD -> Last-Modified header
  Tier 2 : GET + HTML (JSON-LD / meta / body-text regex, sub-link following)
  Tier 3 : Gemini 3.1 Pro reads Tier 2's already-fetched text (replaces Groq)
  Tier 4 : Playwright full render, else Tier 3's Gemini function on rendered text
  Tier 6 : Gemini computer-use (gemini-3.6-flash) — real browser, clicks/scrolls

Tier 5 (Groq compound-beta real-browsing) is deleted outright — Tier 6 already
does real, logged browsing, so it's strictly more capable and transparent.

Every URL gets its own detailed log file under logs/ (every tier attempt, every
Gemini prompt/response/thought, every Tier 6 action) and every output row gets
an auto-generated, plain-English verification_steps recipe (verification_recipes.py)
so the result can be manually reproduced and checked.

Run:
  python3 staleness_pipeline_v2.py --limit 5      # smoke test
  python3 staleness_pipeline_v2.py                 # full run
"""

import argparse
import asyncio
import csv
import json
import os
import random
import re
import time
from collections import defaultdict
from datetime import date as _date, datetime, timezone

import aiohttp
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai
from google.genai import types

from provenance_refresh_extractor import (
    _tier1, _tier2, _extract_from_html, _parse_date, _visible_text,
    classify_url, _fails_llm_recency_guard,
)
from specialized_source_handlers import (    # noqa: E402
    SPECIALIZED_HANDLERS, handle_census_url_vintage, normalize_url, classify_blocker,
)

from tier3_prompt import TIER3_PROMPT_TEMPLATE
from validation_prompt import VALIDATION_PROMPT_TEMPLATE
from computer_use_extractor import tier6_computer_use
import verification_recipes as vr
import bq_io
import gcs_io

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

TIER3_MODEL = "gemini-3.1-pro-preview"
LOGS_DIR    = os.path.join(os.path.dirname(__file__), "logs")

TIER012_CONCURRENCY   = 15   # cheap async HTTP — tiers 0/1/2
GEMINI_TEXT_CONCURRENCY = 5  # tier 3 / tier 4's gemini fallback
TIER6_CONCURRENCY    = 3     # real browser + computer-use — the expensive resource

_api_key = os.environ.get("GEMINI_API_KEY", "")
if not _api_key:
    raise SystemExit("GEMINI_API_KEY is not set (checked ../.env and environment)")
_client = genai.Client(api_key=_api_key)

_TIER3_CONFIG = types.GenerateContentConfig(
    thinking_config=types.ThinkingConfig(include_thoughts=True),
    temperature=0,   # factual extraction, not navigation — match the old Groq Tier 3's determinism
)


def _noop_log(_msg: str) -> None:
    pass


def _safe_filename(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)[:150] or "unnamed"


def _write_log(serial: int, object_id: str, url: str, dataset_ids: list, log_lines: list, result: dict) -> str:
    """Writes the local .log file (useful for local runs) and returns the
    same full text so it can be uploaded to GCS (see gcs_io.upload_log) —
    Cloud Run's local filesystem disappears when the job exits, so that GCS
    copy is the only durable copy of this detail in that environment.

    The filename is prefixed with the same serial number written to the CSV's
    serial_no column, so a row can be matched to its log file by eye without
    comparing the (long, hashed) object_id strings."""
    full_text = (
        f"Serial: {serial}\nURL: {url}\ndataset_ids: {dataset_ids}\n\n"
        + "\n\n".join(log_lines)
        + f"\n\n=== RESULT ===\n"
        f"tier_used: {result.get('tier')}\n"
        f"date: {result.get('date')}\n"
        f"source: {result.get('source')}\n"
        f"tiers_attempted: {result.get('tiers_attempted')}\n"
        f"verification_steps: {result.get('verification_steps')}\n"
    )
    os.makedirs(LOGS_DIR, exist_ok=True)
    path = os.path.join(LOGS_DIR, f"{serial:04d}_{_safe_filename(object_id)}.log")
    with open(path, "w", encoding="utf-8") as f:
        f.write(full_text)
    return full_text


def _tier3_gemini_sync(page_text: str, url: str, log_fn=_noop_log) -> dict | None:
    """Gemini 3.1 Pro text-only reasoning — same role the old Groq
    openai/gpt-oss-120b Tier 3 played: read text we ALREADY fetched (never
    browses itself) and pick the real refresh date out of decoys."""
    try:
        prompt = TIER3_PROMPT_TEMPLATE.format(url=url, page_text=page_text[:500000])
        log_fn(f"=== TIER 3: Gemini 3.1 Pro text reasoning ===\nPrompt sent:\n{prompt}")
        response = _client.models.generate_content(
            model=TIER3_MODEL, contents=prompt, config=_TIER3_CONFIG)
        parts = response.candidates[0].content.parts
        thought_text = "".join(p.text or "" for p in parts if getattr(p, "thought", False))
        final_text = "".join(p.text or "" for p in parts if not getattr(p, "thought", False))
        if thought_text:
            log_fn(f"THOUGHT: {thought_text}")
        log_fn(f"Raw response: {final_text}")
        cleaned = re.sub(r"^```(?:json)?|```$", "", final_text.strip(), flags=re.MULTILINE).strip()
        data = json.loads(cleaned)
        val = _parse_date(str(data.get("date") or ""))
        if val and _fails_llm_recency_guard(val, url):
            log_fn(f"Rejected date={val} — within recency-guard window, likely dynamic 'as of today' pointer")
            val = None
        if val:
            result = {"date": val, "source": data.get("source", "gemini-text"), "tier": 3}
            log_fn(f"RESULT: date={val} source={result['source']!r}")
            return result
        log_fn(f"RESULT: no usable date (raw date field: {data.get('date')!r})")
    except Exception as e:
        log_fn(f"EXCEPTION {type(e).__name__}: {e}")
    return None


def _validate_candidate_sync(url: str, candidate_date: str, page_text: str, log_fn=_noop_log) -> bool:
    """Second-opinion Gemini check for dates found by the cheap body-text
    regex — the only extraction path with no real judgment, so it can be
    fooled by a decoy date sitting near a trigger word (e.g. an unrelated
    news/paper-publication announcement — see PIPELINE_CHANGELOG for the
    web.stanford.edu/deepsolar case that motivated this). Any parse failure
    or exception defaults to False (reject) rather than silently trusting an
    unverified regex hit."""
    try:
        prompt = VALIDATION_PROMPT_TEMPLATE.format(
            url=url, candidate_date=candidate_date, page_text=page_text[:500000])
        log_fn(f"=== VALIDATION: Gemini 3.1 Pro confidence check on body-text candidate {candidate_date} ===\nPrompt sent:\n{prompt}")
        response = _client.models.generate_content(
            model=TIER3_MODEL, contents=prompt, config=_TIER3_CONFIG)
        parts = response.candidates[0].content.parts
        thought_text = "".join(p.text or "" for p in parts if getattr(p, "thought", False))
        final_text = "".join(p.text or "" for p in parts if not getattr(p, "thought", False))
        if thought_text:
            log_fn(f"THOUGHT: {thought_text}")
        log_fn(f"Raw response: {final_text}")
        cleaned = re.sub(r"^```(?:json)?|```$", "", final_text.strip(), flags=re.MULTILINE).strip()
        data = json.loads(cleaned)
        confident = bool(data.get("confident"))
        log_fn(f"VALIDATION RESULT: confident={confident} reason={data.get('reason')!r}")
        return confident
    except Exception as e:
        log_fn(f"VALIDATION EXCEPTION {type(e).__name__}: {e} — treating as not confident")
        return False


async def _tier4(url: str, log_fn=_noop_log) -> dict | None:
    try:
        from playwright.async_api import async_playwright
        log_fn("=== TIER 4: Playwright full render ===")
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(2000)
            html = await page.content()
            await browser.close()

        soup = BeautifulSoup(html, "html.parser")
        text = _visible_text(soup)

        date, src, cadence = _extract_from_html(html, url)
        if date:
            confirmed = True
            if src == "body-text":   # the only extraction path prone to decoy false positives
                confirmed = await asyncio.get_event_loop().run_in_executor(
                    None, _validate_candidate_sync, url, date, text, log_fn)
            if confirmed:
                note = "validated, no full LLM extraction needed" if src == "body-text" else "no LLM needed"
                log_fn(f"RESULT: date={date} via {src!r} ({note})")
                return {"date": date, "source": src + " (playwright)", "tier": 4, "cadence": cadence,
                        "validated": src == "body-text"}
            log_fn(f"Candidate date={date} via {src!r} rejected by validation — falling back to full extraction")

        log_fn("No confident date in rendered HTML, falling back to Gemini 3.1 Pro text reasoning")
        result = await asyncio.get_event_loop().run_in_executor(
            None, _tier3_gemini_sync, text, url, log_fn)
        if result:
            result["tier"] = 4
            result["source"] += " (playwright+gemini)"
            return result
        log_fn("Gemini fallback also found nothing")
    except Exception as e:
        log_fn(f"EXCEPTION {type(e).__name__}: {e}")
    return None


async def process_url(session: aiohttp.ClientSession, url: str, dataset_ids: list, serial: int,
                       global_sem: asyncio.Semaphore, gemini_sem: asyncio.Semaphore,
                       tier6_sem: asyncio.Semaphore) -> dict:
    log_lines = []
    def log_fn(msg: str) -> None:
        log_lines.append(msg)

    t0 = time.time()
    tried = []
    result = {"url": url, "serial": serial, "date": None, "source": None, "tier": None,
              "error": None, "verification_steps": None}

    def _finish():
        result["tiers_attempted"] = ",".join(str(x) for x in tried)
        result["extraction_time_sec"] = round(time.time() - t0, 2)
        if not result["date"] and tried:
            result["error"] = f"no date after tiers {result['tiers_attempted']}"
        if not result["verification_steps"]:
            result["verification_steps"] = vr.recipe_no_date(result["tiers_attempted"] or "none")
        result["detailed_log"] = _write_log(serial, dataset_ids[0], url, dataset_ids, log_lines, result)
        return result

    if classify_url(url) == "catalog":
        log_fn("Classified as catalog/browse URL — no single dataset date exists, skipped")
        result["error"] = "catalog/browse URL — no single dataset date exists"
        result["verification_steps"] = "N/A — catalog/browse URL, no single dataset date exists"
        return _finish()

    if classify_blocker(url) == "not_trackable":
        log_fn("Classified as not externally trackable, skipped")
        result["error"] = "no external source — not programmatically trackable"
        result["verification_steps"] = "N/A — not externally trackable, no external source to check"
        return _finish()

    async with global_sem:
        # Tier 0
        for pattern, handler in SPECIALIZED_HANDLERS:
            if not pattern.search(url):
                continue
            log_fn(f"=== TIER 0: matched handler {handler.__name__} ===")
            tried.append(0)
            r0 = await handler(url, session)
            if r0 and r0.get("_redirect_url"):
                log_fn(f"Handler redirected to {r0['_redirect_url']}")
                r1_redirect = await _tier1(session, r0["_redirect_url"])
                if r1_redirect:
                    r1_redirect["source"] = f"{r1_redirect.get('source', 'Last-Modified header')} (via {r0['_redirect_url']})"
                    result.update(r1_redirect)
                    result["verification_steps"] = vr.recipe_tier1(url, result["date"])
                    return _finish()
            elif r0 and r0.get("date"):
                log_fn(f"Handler found date={r0.get('date')}")
                result.update(r0)
                result["verification_steps"] = vr.recipe_tier0(handler.__name__, url, result["date"])
                return _finish()
            else:
                log_fn("Handler matched but found nothing, falling through to tiers 1-6")
            break

        # Tiers 1 + 2
        log_fn("=== TIER 1: HTTP HEAD Last-Modified ===")
        log_fn("=== TIER 2: GET + HTML parse ===")
        tried += [1, 2]
        r1, r2 = await asyncio.gather(_tier1(session, url), _tier2(session, url))
        html_cache = None
        t2_got_html = False
        # Computed once here (instead of re-parsing later) so it's ready for
        # both Tier 2's validation check below and Tier 3's text-reasoning call.
        soup = None
        text = ""
        if r2:
            html_cache = r2.pop("_html", None)
            t2_got_html = html_cache is not None
            if html_cache:
                soup = BeautifulSoup(html_cache, "html.parser")
                text = _visible_text(soup)
            if r2["date"]:
                confirmed = True
                if r2["source"] == "body-text":   # the only extraction path prone to decoy false positives
                    confirmed = await asyncio.get_event_loop().run_in_executor(
                        None, _validate_candidate_sync, url, r2["date"], text, log_fn)
                    if not confirmed:
                        log_fn(f"Tier 2 candidate date={r2['date']} rejected by validation — continuing to later tiers")
                if confirmed:
                    log_fn(f"Tier 2 found date={r2['date']} via {r2['source']!r}")
                    result.update(r2)
                    result["verification_steps"] = vr.recipe_tier2(url, r2["source"], r2["date"])
                    return _finish()
        if r1 and not t2_got_html:
            log_fn(f"Tier 2 could not fetch HTML; using Tier 1 Last-Modified={r1['date']}")
            result.update(r1)
            result["verification_steps"] = vr.recipe_tier1(url, r1["date"])
            return _finish()
        log_fn("Tiers 1/2 found nothing")

        # Tier 3 — skip if there's no actual text to read (e.g. a JS-rendered
        # page whose plain-HTTP-fetched HTML is just a script shell): an LLM
        # call on an empty string can only ever return null, so it's a wasted
        # round trip — go straight to Tier 4's Playwright render instead.
        if text.strip():
            tried.append(3)
            async with gemini_sem:
                r3 = await asyncio.get_event_loop().run_in_executor(
                    None, _tier3_gemini_sync, text, url, log_fn)
            if r3:
                result.update(r3)
                result["verification_steps"] = vr.recipe_tier3(url, False, r3["source"], r3["date"])
                return _finish()

        # Tier 4
        tried.append(4)
        async with gemini_sem:
            r4 = await _tier4(url, log_fn)
        if r4:
            result.update(r4)
            used_gemini = "(playwright+gemini)" in r4["source"]
            if used_gemini:
                result["verification_steps"] = vr.recipe_tier3(url, True, r4["source"], r4["date"])
            else:
                result["verification_steps"] = vr.recipe_structured(url, r4["source"], r4["date"], rendered=True)
            return _finish()

        # Tier 6
        tried.append(6)
        r6 = await tier6_computer_use(url, tier6_sem, log_fn)
        if r6.get("date"):
            result.update({"date": r6["date"], "source": r6["source"], "tier": 6})
            result["verification_steps"] = vr.recipe_tier6(url, r6.get("action_trace", []), r6["source"], r6["date"])
            return _finish()
        log_fn(f"Tier 6 error: {r6.get('error')}")

        # Absolute last resort: Census ACS vintage year embedded in the URL
        r0_census = await handle_census_url_vintage(url, session)
        if r0_census and r0_census.get("date"):
            log_fn(f"Last-resort Census vintage fallback found {r0_census.get('date')}")
            tried.append(0)
            result.update(r0_census)
            result["verification_steps"] = vr.recipe_tier0("handle_census_url_vintage", url, result["date"])
        return _finish()


def load_urls(csv_path: str) -> dict[str, list[str]]:
    """provenance_bigquery.csv schema: predicate,object_id,value — filter to
    predicate=='sourceDataUrl' rows and dedupe (object_id may repeat exactly)."""
    url_map: dict[str, list[str]] = defaultdict(list)
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("predicate") != "sourceDataUrl":
                continue
            raw_url = (row.get("value") or "").strip().strip('"')
            oid = (row.get("object_id") or "").strip()
            if not raw_url:
                continue
            for url in normalize_url(raw_url):
                if url.startswith("http") and oid not in url_map[url]:
                    url_map[url].append(oid)
    return url_map


# detailed_log is intentionally not a CSV column (would bloat the file;
# local logs/<serial>_<object_id>.log already has it, matched by serial_no)
# — extrasaction="ignore" lets the same row dict feed both the CSV writer
# and (in bq mode) bq_io.write_results.
CSV_FIELDNAMES = ["serial_no", "object_id", "url", "last_refresh_date", "date_method",
                  "date_source", "tier_used", "date_found", "verification_steps",
                  "tiers_attempted", "tier_failed_reason", "extraction_time_sec"]


def _load_completed_urls(output_csv: str) -> tuple[set[str], int]:
    """For --resume: reads an existing output CSV from a prior (possibly
    killed) run and returns (URLs already processed, highest serial_no used)
    so a re-run skips finished work instead of redoing it, and new serials
    don't collide with existing log filenames."""
    if not os.path.exists(output_csv):
        return set(), 0
    completed, max_serial = set(), 0
    with open(output_csv, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            completed.add(row.get("url", ""))
            try:
                max_serial = max(max_serial, int(row.get("serial_no") or 0))
            except ValueError:
                pass
    return completed, max_serial


async def main(input_csv: str | None, output_csv: str, limit: int | None,
               source: str, billing_project: str, random_sample: bool = False,
               write_bq: bool = True, resume: bool = False):
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    if source == "bq":
        if write_bq:
            bq_io.ensure_table(billing_project)   # only needed if we're about to write to it
        url_map = bq_io.load_urls_from_bq(billing_project)
    else:
        if not input_csv:
            raise SystemExit("--source csv requires --input <path>")
        url_map = load_urls(input_csv)
    all_urls = list(url_map.keys())
    if limit:
        all_urls = random.sample(all_urls, min(limit, len(all_urls))) if random_sample else all_urls[:limit]

    start_serial = 1
    append_mode = False
    if resume:
        completed_urls, max_serial = _load_completed_urls(output_csv)
        before = len(all_urls)
        all_urls = [u for u in all_urls if u not in completed_urls]
        start_serial = max_serial + 1
        append_mode = bool(completed_urls)
        print(f"--resume: {before - len(all_urls)} URLs already in {output_csv}, {len(all_urls)} remaining")

    print(f"Loaded {len(url_map)} unique URLs ({sum(len(v) for v in url_map.values())} dataset ids); running {len(all_urls)}")

    global_sem = asyncio.Semaphore(TIER012_CONCURRENCY)
    gemini_sem = asyncio.Semaphore(GEMINI_TEXT_CONCURRENCY)
    tier6_sem  = asyncio.Semaphore(TIER6_CONCURRENCY)
    connector = aiohttp.TCPConnector(ssl=False, limit=TIER012_CONCURRENCY)

    # Written per-URL as each one finishes (not batched to the very end) —
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
        tasks = [process_url(session, url, url_map[url], serial, global_sem, gemini_sem, tier6_sem)
                 for serial, url in enumerate(all_urls, start=start_serial)]
        done = 0
        for coro in asyncio.as_completed(tasks):
            r = await coro
            done += 1
            tier_label = f"T{r['tier']}" if r.get("tier") is not None else "miss"
            print(f"  [{done}/{len(all_urls)}] {tier_label}  {r['date'] or '—':<12}  {r['url'][:70]}")

            # Upload once per URL (not once per dataset_id sharing it) — all
            # rows for this URL point at the same log, same as the local
            # logs/<serial>_<object_id>.log file already does.
            log_gcs_uri = ""
            if source == "bq" and write_bq:
                try:
                    log_gcs_uri = gcs_io.upload_log(
                        billing_project, url_map[r["url"]][0], r["serial"], r.get("detailed_log", ""))
                except Exception as e:
                    print(f"  [gcs_io] log upload failed for {r['url'][:70]} ({type(e).__name__}: {e}) — "
                          f"continuing without it (local logs/ still has the full log)")

            bq_rows_this_url = []
            for oid in url_map[r["url"]]:
                row = {
                    "serial_no":           r["serial"],
                    "object_id":           oid,
                    "url":                 r["url"],
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
                bq_rows_this_url.append(row)
            csv_file.flush()   # survive a kill/sleep between completions, not just at the end
            if source == "bq" and write_bq:
                bq_io.write_results(billing_project, run_id, bq_rows_this_url)

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
    args = ap.parse_args()
    asyncio.run(main(args.input, args.output, args.limit, args.source, args.billing_project,
                      args.random, not args.no_bq_write))
