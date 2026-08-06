"""
Tier 1 — Gemini Computer Use, with a Tier 2 download+file-inspection hand-off.

Runs AFTER Tier 0's domain-specific handlers have already tried and failed for
a URL. Drives a real Playwright browser with Gemini's computer-use tool — it
looks at screenshots and decides where to click/scroll/navigate — to find the
last-refresh date visually, for pages where no direct handler could locate one.

If, mid-session, the browser starts an actual file download (rather than
rendering a page) instead of trying to make the model reason about a page it
fundamentally cannot open, the browser session ends there and now hands off to
plain-Gemini-API file inspection (file_date_extractor.py, repo root) to read
the downloaded file directly and extract the real last-observation date — see
tier2_download_and_inspect() below. This whole file used to also gate on Tiers
1-5 (HTTP HEAD, HTML parse, Gemini text reasoning, Playwright static render,
Groq real-browsing); all of those were removed outright as low-yield. Tier 2
also used to shell out to a third-party coding-agent CLI (pi-coding-agent) —
that was replaced with a plain two-step Gemini API call (no agent, no
subprocess) after that tool was found to violate policy for use on Google
source/data.

Setup:
  pip install google-genai playwright
  playwright install chromium
  export GEMINI_API_KEY=...   (or set it in .env)

Run:
  python3 computer_use_extractor.py \\
      --input provenance_refresh_dates.json \\
      --output computer_use_results.json \\
      --limit 10
"""

import argparse
import asyncio
import json
import os
import re
import shutil
import tempfile
from datetime import date as _date

from dotenv import load_dotenv
from google import genai
from google.genai import types

from computer_use_prompt import TASK_PROMPT_TEMPLATE
from file_date_extractor import extract_date_from_file
from provenance_refresh_extractor import _parse_date  # same strict date parser every other tier uses

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

MODEL        = "gemini-3.6-flash"
MAX_STEPS    = 40          # actions per URL before giving up — generous now that cost isn't the constraint;
                           # still bounded so one pathological URL can't stall the whole batch run indefinitely
CONCURRENCY  = 3           # real browsers + a slow multimodal model — keep this low
                           # (the pipeline itself uses TIER1_CONCURRENCY instead of this
                           # module-level default — see staleness_pipeline_v2.py)
VIEWPORT     = {"width": 1440, "height": 900}

_api_key = os.environ.get("GEMINI_API_KEY", "")
if not _api_key:
    raise SystemExit("GEMINI_API_KEY is not set (checked .env and environment)")
_client = genai.Client(api_key=_api_key)

_TOOL_CONFIG = types.GenerateContentConfig(
    tools=[types.Tool(computer_use=types.ComputerUse(
        environment=types.Environment.ENVIRONMENT_BROWSER,
    ))],
    thinking_config=types.ThinkingConfig(include_thoughts=True),
)


def _denorm(val: int, size: int) -> int:
    """Model coordinates are normalized to 0-1000; scale to actual pixels."""
    return int(val / 1000 * size)


async def _execute(page, name: str, args: dict) -> str | None:
    """Executes one predefined computer-use action. Returns an error string
    on failure, None on success — either way the loop continues, since a
    failed action just shows up as an unchanged screenshot on the next turn."""
    w, h = VIEWPORT["width"], VIEWPORT["height"]
    try:
        if name == "open_web_browser":
            pass
        elif name == "navigate":
            await page.goto(args["url"], wait_until="domcontentloaded", timeout=20000)
        elif name == "go_back":
            await page.go_back(timeout=10000)
        elif name == "go_forward":
            await page.go_forward(timeout=10000)
        elif name in ("wait_5_seconds", "wait"):
            await page.wait_for_timeout(5000)
        elif name in ("click_at", "click", "double_click", "triple_click", "right_click", "middle_click"):
            await page.mouse.click(_denorm(args["x"], w), _denorm(args["y"], h))
        elif name in ("hover_at", "move"):
            await page.mouse.move(_denorm(args["x"], w), _denorm(args["y"], h))
        elif name == "type_text_at":
            await page.mouse.click(_denorm(args["x"], w), _denorm(args["y"], h))
            await page.keyboard.type(args["text"])
            if args.get("press_enter"):
                await page.keyboard.press("Enter")
        elif name == "type":
            await page.keyboard.type(args["text"])
            if args.get("press_enter"):
                await page.keyboard.press("Enter")
        elif name in ("key_combination", "hotkey"):
            keys = args.get("keys") or args.get("keys_combination", "")
            await page.keyboard.press("+".join(keys.split()))
        elif name in ("press_key", "key_down", "key_up"):
            await page.keyboard.press(args.get("key", ""))
        elif name == "scroll_document":
            dy = {"down": 600, "up": -600}.get(args.get("direction"), 0)
            dx = {"right": 600, "left": -600}.get(args.get("direction"), 0)
            await page.mouse.wheel(dx, dy)
        elif name in ("scroll_at", "scroll"):
            if "x" in args and "y" in args:
                await page.mouse.move(_denorm(args["x"], w), _denorm(args["y"], h))
            mag = args.get("magnitude", 600)
            dy = {"down": mag, "up": -mag}.get(args.get("direction"), 0)
            dx = {"right": mag, "left": -mag}.get(args.get("direction"), 0)
            await page.mouse.wheel(dx, dy)
        elif name == "drag_and_drop":
            await page.mouse.move(_denorm(args["x"], w), _denorm(args["y"], h))
            await page.mouse.down()
            await page.mouse.move(_denorm(args["destination_x"], w), _denorm(args["destination_y"], h))
            await page.mouse.up()
        elif name == "take_screenshot":
            pass
        elif name == "search":
            await page.goto("https://www.google.com", wait_until="domcontentloaded", timeout=20000)
        else:
            return f"unsupported action: {name}"
    except Exception as e:
        return f"{type(e).__name__}: {e}"
    return None


def _parse_final_answer(text: str) -> dict | None:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    val = str(data.get("date") or "").strip()
    if not val or val.upper() in ("NULL", "NONE", "N/A"):
        return None
    # Bare years are accepted here (e.g. a wide-format table's column header
    # like "Population (2013)") — same reasoning as Tier 2's
    # _validate_tier2_date(): a year read directly off the page/table is the
    # real answer, not an ambiguous scraped decoy.
    if re.fullmatch(r"\d{4}", val):
        year = int(val)
        return {"date": val, "source": data.get("source", "computer-use")} if 2000 <= year <= _date.today().year + 1 else None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", val):
        return None
    if not (2000 <= int(val[:4]) <= _date.today().year + 1) or val > _date.today().isoformat():
        return None
    return {"date": val, "source": data.get("source", "computer-use")}


def _noop_log(_msg: str) -> None:
    pass


async def _screenshot_with_retry(page, log_fn=_noop_log, retries: int = 2, delay: float = 1.5):
    """page.screenshot() intermittently fails on transient rendering hiccups
    (e.g. a font-load timeout inside Chromium's screenshot protocol) — this
    killed whole Tier 1 attempts mid-exploration with budget still remaining
    (confirmed live: a metadata-modal case died here with 12 steps left).
    Retries a couple of times before giving up. Returns None — not raising —
    when the page/browser is genuinely gone (TargetClosedError), since no
    amount of retrying fixes that; the caller decides how to end gracefully."""
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return await page.screenshot(type="png")
        except Exception as e:
            last_exc = e
            if "closed" in str(e).lower() or "TargetClosedError" in type(e).__name__:
                log_fn(f"Screenshot failed, page/browser is closed — not retrying: {type(e).__name__}: {e}")
                return None
            log_fn(f"Screenshot attempt {attempt + 1}/{retries + 1} failed ({type(e).__name__}: {e}), retrying in {delay}s...")
            await asyncio.sleep(delay)
    log_fn(f"Screenshot failed after {retries + 1} attempts: {type(last_exc).__name__}: {last_exc}")
    return None


async def _save_download(download, log_fn=_noop_log) -> tuple[str | None, str]:
    """Saves a Playwright Download to a fresh temp directory — must be called
    while the browser/context that produced it is still open. Returns
    (filepath, tmpdir); filepath is None if saving failed, but tmpdir is
    always returned so the caller can clean it up either way."""
    tmpdir = tempfile.mkdtemp(prefix="tier2_download_")
    try:
        filename = download.suggested_filename or "downloaded_file"
        filepath = os.path.join(tmpdir, filename)
        await download.save_as(filepath)
        log_fn(f"=== TIER 2: real file download detected, saved to {filepath} ===")
        return filepath, tmpdir
    except Exception as e:
        log_fn(f"Failed to save detected download: {type(e).__name__}: {e}")
        return None, tmpdir


def _validate_tier2_date(raw: str) -> "str | None":
    """Tier 2's own validation — same as _parse_date() for anything with
    real day/month precision, but ALSO accepts a bare 4-digit year as-is
    (still range-checked). Every OTHER tier rejects bare years via
    _parse_date() because a lone year scraped from a page is usually an
    ambiguous decoy (a copyright notice, etc.). That reasoning doesn't apply
    to Tier 2's wide-format case: the year there is the file's own real
    column header — genuine tabular data, not a scraped decoy — so rejecting
    it the same way would silently defeat the reason wide-format support
    exists in Tier 2 at all."""
    raw = raw.strip()
    if re.fullmatch(r"\d{4}", raw):
        year = int(raw)
        return raw if (2000 <= year <= _date.today().year + 1) else None
    return _parse_date(raw)


def _inspect_downloaded_file(filepath: str, log_fn=_noop_log) -> dict:
    """Hands an already-saved, already-downloaded file to
    file_date_extractor.extract_date_from_file() — two plain Gemini API calls
    (pick the right column, then identify its max from every distinct
    full-file value), no agent/subprocess involved. Call only after the
    browser has been closed; this works on the file directly via pandas, not
    the browser. Returns {"date", "source", "error", "column_used"} — no
    "tier"/"steps_used"/"action_trace", since the caller (tier1_computer_use)
    already tracks those and merges this in."""
    try:
        last_obs_date, column_used, files_checked = extract_date_from_file(filepath)
        log_fn(f"file inspection returned last_obs_date={last_obs_date!r} column={column_used!r} "
               f"files_checked={files_checked}")
    except Exception as e:
        log_fn(f"file inspection EXCEPTION {type(e).__name__}: {e}")
        return {"date": None, "source": None, "error": f"file inspection failed: {type(e).__name__}: {e}"}

    if not last_obs_date or last_obs_date == "not_possible":
        return {"date": None, "source": None,
                "error": "could not determine a date from the downloaded file"}

    # Bare years ARE accepted here (see _validate_tier2_date) — unlike every
    # other tier, a wide-format year is the file's own real column header,
    # not an ambiguous scraped decoy.
    val = _validate_tier2_date(str(last_obs_date))
    if not val:
        log_fn(f"date {last_obs_date!r} rejected by _validate_tier2_date (out of range / unparseable) — treating as a miss")
        return {"date": None, "source": None,
                "error": f"returned an unusable date: {last_obs_date!r}"}

    return {"date": val, "source": f"downloaded file, column '{column_used}'",
            "error": None, "column_used": column_used}


async def tier1_computer_use(url: str, sem: asyncio.Semaphore, log_fn=_noop_log) -> dict:
    """Drives a real browser with Gemini's computer-use tool.
    Returns {"date", "source", "tier", "steps_used", "error", "action_trace"} —
    date/source are None on failure. action_trace is a list of
    {"step", "action", "args", "intent", "error"} dicts, one per action taken,
    used both for the per-URL log and for building the Tier 1 verification
    recipe (see verification_recipes.recipe_tier1_computer_use).

    If a real file download starts mid-session (rather than a page
    rendering), the browser session ends there and this hands off to the pi
    coding agent instead — the returned dict then has "tier": 2 and
    "downloaded_file"/"column_used" fields instead of a computer-use source."""
    from playwright.async_api import async_playwright

    async with sem:
        log_fn(f"=== TIER 1: Gemini computer-use ({MODEL}) ===")
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport=VIEWPORT)

            # Checked once per loop iteration (not polled separately) so the
            # common no-download path pays zero extra latency. A sync append
            # here is safe — save_as() is awaited later, once we've decided
            # to hand off.
            pending_downloads: list = []
            page.on("download", lambda d: pending_downloads.append(d))

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(1500)
                log_fn(f"Opened {url}")
            except Exception as e:
                log_fn(f"Initial navigation failed ({type(e).__name__}: {e}) — letting the model see whatever loaded")

            if pending_downloads:
                log_fn("Navigating directly to this URL triggered a file download — handing off to Tier 2")
                filepath, tmpdir = await _save_download(pending_downloads[0], log_fn)
                await browser.close()
                if filepath is None:
                    shutil.rmtree(tmpdir, ignore_errors=True)
                    return {"date": None, "source": None, "tier": 1, "steps_used": 0,
                            "error": "download detected but could not be saved", "action_trace": []}
                try:
                    inspected = await asyncio.get_event_loop().run_in_executor(
                        None, _inspect_downloaded_file, filepath, log_fn)
                finally:
                    shutil.rmtree(tmpdir, ignore_errors=True)
                return {**inspected, "tier": 2 if inspected.get("date") else 1,
                        "steps_used": 0, "action_trace": [], "downloaded_file": filepath}

            shot = await _screenshot_with_retry(page, log_fn)
            if shot is None:
                await browser.close()
                return {"date": None, "source": None, "tier": 1, "steps_used": 0,
                        "error": "browser/page closed unexpectedly before the first screenshot",
                        "action_trace": []}
            prompt_text = TASK_PROMPT_TEMPLATE.format(url=url, max_steps=MAX_STEPS)
            log_fn(f"Prompt sent:\n{prompt_text}")
            contents = [types.Content(role="user", parts=[
                types.Part(text=prompt_text),
                types.Part.from_bytes(data=shot, mime_type="image/png"),
            ])]

            result = {"date": None, "source": None, "tier": 1, "steps_used": 0,
                      "error": None, "action_trace": []}
            # Set when the loop below detects a download and breaks out —
            # checked once after the browser is closed (see the finally block)
            # so there is only ever one place that closes the browser.
            download_filepath, download_tmpdir = None, None
            try:
                for step in range(MAX_STEPS):
                    response = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: _client.models.generate_content(
                            model=MODEL, contents=contents, config=_TOOL_CONFIG),
                    )
                    candidate = response.candidates[0]
                    contents.append(candidate.content)
                    result["steps_used"] = step + 1

                    for pt in candidate.content.parts:
                        if getattr(pt, "thought", False) and pt.text:
                            log_fn(f"[step {step + 1}] THOUGHT: {pt.text}")

                    fn_parts = [pt for pt in candidate.content.parts if pt.function_call]
                    if not fn_parts:
                        final_text = "".join(pt.text or "" for pt in candidate.content.parts if not pt.thought)
                        log_fn(f"[step {step + 1}] Final response (no function call): {final_text}")
                        parsed = _parse_final_answer(final_text)
                        if parsed:
                            result.update(parsed)
                            log_fn(f"RESULT: date={parsed['date']} source={parsed['source']!r}")
                        else:
                            result["error"] = f"model stopped without a parseable answer: {final_text[:200]!r}"
                            log_fn(f"RESULT: no parseable answer")
                        break

                    fc = fn_parts[0].function_call
                    fc_args = dict(fc.args or {})
                    intent = fc_args.get("intent")
                    log_fn(f"[step {step + 1}] ACTION: {fc.name}({fc_args}){' intent=' + intent if intent else ''}")

                    safety = fc_args.get("safety_decision") or {}
                    if safety.get("decision") == "blocked":
                        err = f"action blocked by model safety policy: {safety.get('explanation')}"
                        log_fn(f"[step {step + 1}] SAFETY: {err}")
                    else:
                        err = await _execute(page, fc.name, fc_args)
                        if err:
                            log_fn(f"[step {step + 1}] action error: {err}")

                    result["action_trace"].append({
                        "step": step + 1, "action": fc.name, "args": fc_args,
                        "intent": intent, "error": err,
                    })

                    if pending_downloads:
                        log_fn(f"[step {step + 1}] Detected a real file download mid-session — "
                               f"ending the browser session, handing off to Tier 2")
                        download_filepath, download_tmpdir = await _save_download(pending_downloads[0], log_fn)
                        break

                    new_shot = await _screenshot_with_retry(page, log_fn)
                    if new_shot is None:
                        log_fn(f"[step {step + 1}] Could not capture a screenshot after this action "
                               f"(page/browser closed) — ending attempt with progress so far")
                        result["error"] = f"page/browser closed unexpectedly at step {step + 1}"
                        break
                    shot = new_shot
                    response_payload = {"error": err} if err else {"url": page.url}
                    if safety.get("decision") == "require_confirmation":
                        # No human is in the loop in this unattended batch pipeline; every
                        # action here is read-only browsing (clicks/scrolls/navigation) to
                        # locate a public last-updated date, never a destructive/financial
                        # one — so we auto-acknowledge rather than blocking the whole run.
                        # Logged explicitly so this auto-approval is visible on audit.
                        log_fn(f"[step {step + 1}] SAFETY: auto-acknowledging require_confirmation "
                               f"({safety.get('explanation')}) — unattended read-only browsing task")
                        response_payload["safety_acknowledgement"] = True
                    fr = types.FunctionResponse(
                        name=fc.name,
                        response=response_payload,
                        parts=[types.FunctionResponsePart(
                            inline_data=types.FunctionResponseBlob(mime_type="image/png", data=shot))],
                    )
                    contents.append(types.Content(role="user", parts=[types.Part(function_response=fr)]))
                else:
                    result["error"] = f"exceeded {MAX_STEPS} actions without a final answer"
                    log_fn(f"RESULT: exceeded {MAX_STEPS} actions without a final answer")
            except Exception as e:
                result["error"] = f"{type(e).__name__}: {e}"
                log_fn(f"RESULT: EXCEPTION {type(e).__name__}: {e}")
            finally:
                await browser.close()

            if download_tmpdir is not None:
                if download_filepath is None:
                    shutil.rmtree(download_tmpdir, ignore_errors=True)
                    result["error"] = "download detected but could not be saved"
                    return result
                try:
                    inspected = await asyncio.get_event_loop().run_in_executor(
                        None, _inspect_downloaded_file, download_filepath, log_fn)
                finally:
                    shutil.rmtree(download_tmpdir, ignore_errors=True)
                result.update(inspected)
                result["downloaded_file"] = download_filepath
                if inspected.get("date"):
                    result["tier"] = 2
                return result

            return result


async def main(input_path: str, output_path: str, limit: int | None):
    with open(input_path) as f:
        existing = json.load(f)

    # Tier 1 targets URLs, not dataset ids — many ids can share one failed URL.
    misses_by_url: dict[str, list[str]] = {}
    for did, v in existing.items():
        if not v.get("date_found") and v.get("url"):
            misses_by_url.setdefault(v["url"], []).append(did)

    urls = list(misses_by_url.keys())
    if limit:
        urls = urls[:limit]
    print(f"{len(misses_by_url)} unique failed URLs, running Tier 1 on {len(urls)}")

    sem = asyncio.Semaphore(CONCURRENCY)
    results = await asyncio.gather(*[tier1_computer_use(u, sem) for u in urls])

    output = dict(existing)
    resolved = 0
    for url, r in zip(urls, results):
        if r["date"]:
            resolved += 1
        for did in misses_by_url[url]:
            output[did].update({
                "last_refresh_date":  r["date"] or output[did]["last_refresh_date"],
                "date_source":        r["source"] or output[did]["date_source"],
                "tier_used":          r["tier"] if r["date"] else output[did]["tier_used"],
                "date_found":         bool(r["date"]) or output[did]["date_found"],
                "tier1_steps_used":   r["steps_used"],
                "tier1_error":        r["error"],
            })
        print(f"  {'FOUND ' + r['date'] if r['date'] else 'miss  '}  ({r['steps_used']} steps)  {url[:70]}")

    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nResolved {resolved}/{len(urls)} via Tier 1/2 -> {output_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--input",  default=os.path.join(os.path.dirname(__file__), "provenance_refresh_dates.json"))
    ap.add_argument("--output", default=os.path.join(os.path.dirname(__file__), "computer_use_results.json"))
    ap.add_argument("--limit",  type=int, default=None, help="cap number of URLs (for testing)")
    args = ap.parse_args()
    asyncio.run(main(args.input, args.output, args.limit))
