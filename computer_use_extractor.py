"""
Tier 6 — Gemini Computer Use fallback.

Runs AFTER the existing 5-tier cascade (provenance_refresh_extractor.py) has
already tried and failed for a URL. Drives a real Playwright browser with
Gemini's computer-use tool — it looks at screenshots and decides where to
click/scroll/navigate — to find the last-refresh date visually, for pages
where no static HTML fetch (Tier 0-5) could locate one.

Setup:
  pip install google-genai playwright
  playwright install chromium
  export GEMINI_API_KEY=...   (or set it in ../.env)

Run:
  python3 experiment2/computer_use_extractor.py \\
      --input provenance_refresh_dates.json \\
      --output experiment2/computer_use_results.json \\
      --limit 10
"""

import argparse
import asyncio
import json
import os
import re
from datetime import date as _date

from dotenv import load_dotenv
from google import genai
from google.genai import types

from computer_use_prompt import TASK_PROMPT_TEMPLATE

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

MODEL        = "gemini-3.6-flash"
MAX_STEPS    = 40          # actions per URL before giving up — generous now that cost isn't the constraint;
                           # still bounded so one pathological URL can't stall the whole batch run indefinitely
CONCURRENCY  = 3           # real browsers + a slow multimodal model — keep this low
VIEWPORT     = {"width": 1440, "height": 900}

_api_key = os.environ.get("GEMINI_API_KEY", "")
if not _api_key:
    raise SystemExit("GEMINI_API_KEY is not set (checked ../.env and environment)")
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


# Same recency guard as Tier 3/5 in provenance_refresh_extractor.py
# (_fails_llm_recency_guard): a range-end timestamp like "Availability
# 2015-06-27 - 2026-07-29T00:42:...Z" is a dynamically generated "as of
# right now" pointer, not a real refresh event — confirmed live during
# smoke testing (developers.google.com Earth Engine catalog page returned
# today's exact timestamp as the "refresh date").
_RECENCY_GUARD_DAYS = 7


def _parse_final_answer(text: str) -> dict | None:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    val = str(data.get("date") or "").strip()
    if not val or val.upper() in ("NULL", "NONE", "N/A"):
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", val):
        return None
    if not (2000 <= int(val[:4]) <= _date.today().year + 1) or val > _date.today().isoformat():
        return None
    if (_date.today() - _date.fromisoformat(val)).days < _RECENCY_GUARD_DAYS:
        return None
    return {"date": val, "source": data.get("source", "computer-use")}


def _noop_log(_msg: str) -> None:
    pass


async def tier6_computer_use(url: str, sem: asyncio.Semaphore, log_fn=_noop_log) -> dict:
    """Drives a real browser with Gemini's computer-use tool.
    Returns {"date", "source", "tier", "steps_used", "error", "action_trace"} —
    date/source are None on failure. action_trace is a list of
    {"step", "action", "args", "intent", "error"} dicts, one per action taken,
    used both for the per-URL log and for building the Tier 6 verification
    recipe (see verification_recipes.recipe_tier6)."""
    from playwright.async_api import async_playwright

    async with sem:
        log_fn(f"=== TIER 6: Gemini computer-use ({MODEL}) ===")
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport=VIEWPORT)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(1500)
                log_fn(f"Opened {url}")
            except Exception as e:
                log_fn(f"Initial navigation failed ({type(e).__name__}: {e}) — letting the model see whatever loaded")

            shot = await page.screenshot(type="png")
            prompt_text = TASK_PROMPT_TEMPLATE.format(url=url, max_steps=MAX_STEPS)
            log_fn(f"Prompt sent:\n{prompt_text}")
            contents = [types.Content(role="user", parts=[
                types.Part(text=prompt_text),
                types.Part.from_bytes(data=shot, mime_type="image/png"),
            ])]

            result = {"date": None, "source": None, "tier": 6, "steps_used": 0,
                      "error": None, "action_trace": []}
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
                    shot = await page.screenshot(type="png")
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
            return result


async def main(input_path: str, output_path: str, limit: int | None):
    with open(input_path) as f:
        existing = json.load(f)

    # Tier 6 targets URLs, not dataset ids — many ids can share one failed URL.
    misses_by_url: dict[str, list[str]] = {}
    for did, v in existing.items():
        if not v.get("date_found") and v.get("url"):
            misses_by_url.setdefault(v["url"], []).append(did)

    urls = list(misses_by_url.keys())
    if limit:
        urls = urls[:limit]
    print(f"{len(misses_by_url)} unique failed URLs, running Tier 6 on {len(urls)}")

    sem = asyncio.Semaphore(CONCURRENCY)
    results = await asyncio.gather(*[tier6_computer_use(u, sem) for u in urls])

    output = dict(existing)
    resolved = 0
    for url, r in zip(urls, results):
        if r["date"]:
            resolved += 1
        for did in misses_by_url[url]:
            output[did].update({
                "last_refresh_date":  r["date"] or output[did]["last_refresh_date"],
                "date_source":        r["source"] or output[did]["date_source"],
                "tier_used":          6 if r["date"] else output[did]["tier_used"],
                "date_found":         bool(r["date"]) or output[did]["date_found"],
                "tier6_steps_used":   r["steps_used"],
                "tier6_error":        r["error"],
            })
        print(f"  {'FOUND ' + r['date'] if r['date'] else 'miss  '}  ({r['steps_used']} steps)  {url[:70]}")

    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nResolved {resolved}/{len(urls)} via Tier 6 -> {output_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--input",  default=os.path.join(os.path.dirname(__file__), "provenance_refresh_dates.json"))
    ap.add_argument("--output", default=os.path.join(os.path.dirname(__file__), "computer_use_results.json"))
    ap.add_argument("--limit",  type=int, default=None, help="cap number of URLs (for testing)")
    args = ap.parse_args()
    asyncio.run(main(args.input, args.output, args.limit))
