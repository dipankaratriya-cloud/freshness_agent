"""
Auto-generated, plain-English "how did we get this date" recipes.

Every row in the final CSV gets one of these, built deterministically from
data already captured during the run — never by an LLM writing it after the
fact, and never by re-parsing the per-URL log files. Each function mirrors
exactly what that tier actually does in staleness_pipeline_v2.py, so a human
can follow the numbered steps and reproduce the result by hand.
"""


def recipe_tier0(handler_name: str, url: str, date: str) -> str:
    return (
        f"STEP 1: Matched domain-specific handler `{handler_name}` for `{url}`. "
        f"STEP 2: Handler made its own direct API/endpoint call (no generic HTML scraping). "
        f"STEP 3: Handler returned date -> {date}."
    )


def recipe_tier1(url: str, date: str) -> str:
    return (
        f"STEP 1: Send an HTTP HEAD request to `{url}`. "
        f"STEP 2: Read the `Last-Modified` response header. "
        f"STEP 3: Parsed value -> {date}."
    )


def _tier2_field_description(source_label: str) -> str:
    label = source_label.split(" (sub:")[0]
    if label.startswith("json-ld:"):
        key = label.split(":", 1)[1]
        return f"Parse `<script type=\"application/ld+json\">` and read the `{key}` field"
    if label.startswith("meta:"):
        name = label.split(":", 1)[1]
        return f"Read the `<meta name=\"{name}\">` tag's `content` attribute"
    if label.startswith("body-text"):
        return "Scan the visible page text for a \"last updated/refreshed/as of\" phrase"
    return f"Extract via `{label}`"


def recipe_structured(url: str, source_label: str, date: str, rendered: bool = False) -> str:
    """Covers Tier 2 (plain GET) AND Tier 4's direct-extraction path (Playwright
    render, then the SAME structured/regex parsing found a date with no LLM
    call at all) — the fetch mechanism differs, the extraction method doesn't."""
    fetch_step = (
        f"STEP 1: Render `{url}` with headless Chromium (Playwright), since a plain "
        f"HTTP GET found no date"
        if rendered else
        f"STEP 1: GET `{url}` (plain HTTP, no browser rendering)"
    )
    clean_label = source_label.replace(" (playwright)", "")
    sub_note = ""
    if " (sub:" in clean_label:
        clean_label, sub_url = clean_label.split(" (sub:", 1)
        sub_url = sub_url.rstrip(")")
        sub_note = f" (found on a linked sub-page, not the main URL: `{sub_url}`)"
    return (
        f"{fetch_step}. "
        f"STEP 2: {_tier2_field_description(clean_label)}{sub_note}. "
        f"STEP 3: Parsed value -> {date}."
    )


def recipe_tier2(url: str, source_label: str, date: str) -> str:
    return recipe_structured(url, source_label, date, rendered=False)


def recipe_tier3(url: str, rendered: bool, source_snippet: str, date: str) -> str:
    fetch_step = (
        f"STEP 1: GET `{url}` failed to yield a date via structured parsing, so render it "
        f"with headless Chromium (Playwright) instead"
        if rendered else
        f"STEP 1: GET `{url}` (plain HTTP)"
    )
    return (
        f"{fetch_step}. "
        f"STEP 2: Strip `<script>`/`<style>` tags and take the visible page text. "
        f"STEP 3: Ask Gemini 3.1 Pro to identify the dataset's last-refresh date, "
        f"excluding copyright years, CMS edit timestamps, and other decoys "
        f"(see tier3_prompt.py). "
        f"STEP 4: Model cited '{source_snippet}' -> {date}."
    )


def recipe_tier6(url: str, action_trace: list, source: str, date: str) -> str:
    steps = [f"STEP 1: Open `{url}` in a real headless browser."]
    for i, action in enumerate(action_trace, start=2):
        name = action.get("action", "?")
        intent = action.get("intent")
        desc = f"STEP {i}: performed `{name}`"
        if intent:
            desc += f" (intent: {intent})"
        if action.get("error"):
            desc += f" -- failed: {action['error']}"
        steps.append(desc)
    steps.append(
        f"STEP {len(action_trace) + 2}: Gemini computer-use agent read the page/screenshot "
        f"and cited '{source}' -> {date}."
    )
    return " ".join(steps)


def recipe_no_date(tiers_attempted: str) -> str:
    return (
        f"No verification steps — tiers [{tiers_attempted}] were all attempted "
        f"and none found a usable date."
    )


def method_label(tier_used, date_source: str) -> str:
    """Short, human-scannable answer to "how was this found" — a CSV column
    version of the same tier/source info recipe_* already turns into full
    step-by-step prose."""
    if tier_used is None or tier_used == "":
        return "Not found"
    tier_used = int(tier_used)
    source = date_source or ""

    if tier_used == 0:
        return "Direct API / domain-specific handler"
    if tier_used == 1:
        return "HTTP Last-Modified header"
    if tier_used == 3:
        return "Gemini text reasoning (LLM)"
    if tier_used in (2, 4):
        if "(playwright+gemini)" in source:
            return "Gemini text reasoning (LLM, rendered page)"
        clean = source.replace(" (playwright)", "").split(" (sub:")[0]
        if clean.startswith("json-ld:"):
            base = "JSON-LD structured data"
        elif clean.startswith("meta:"):
            base = "HTML meta tag"
        elif clean.startswith("body-text"):
            base = "Regex pattern match (Gemini-validated)"
        else:
            base = "Structured field match"
        return f"{base}, rendered page" if tier_used == 4 else base
    if tier_used == 6:
        return "Gemini computer-use (browser navigation)"
    return f"Tier {tier_used}"
