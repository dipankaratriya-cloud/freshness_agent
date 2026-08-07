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


def recipe_tier1_computer_use(url: str, action_trace: list, source: str, date: str) -> str:
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


def recipe_tier2_download(url: str, action_trace: list, filepath: str, date: str, column_used: str) -> str:
    """Tier 2 is a hand-off from within a live Tier 1 session, not an
    independent fallback — this continues Tier 1's own action-trace
    numbering before describing the hand-off itself."""
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
    n = len(action_trace) + 2
    steps.append(f"STEP {n}: a real file download started (`{filepath}`) instead of a rendered "
                  f"page — the browser session ended and control handed off to plain-Gemini-API file inspection.")
    steps.append(f"STEP {n + 1}: read the downloaded file, identified column `{column_used}` -> {date}.")
    return " ".join(steps)


def recipe_no_date(tiers_attempted: str) -> str:
    return (
        f"No verification steps — tiers [{tiers_attempted}] were all attempted "
        f"and none found a usable date."
    )


def recipe_fallback_url(sourcedataurl: str, tiers_a: str,
                         provenance_url: str, tiers_b: str,
                         recipe_b: str) -> str:
    """The sourceDataUrl attempt found nothing at all, so provenance_url was
    tried as a fallback and succeeded. recipe_b is whatever recipe_* already
    fired for that second attempt (recipe_tier0/tier1/structured/tier3/tier6)
    — this just prepends the failed first attempt for full transparency,
    matching every other recipe_* function's "reproducible by hand" ethos."""
    return (
        f"ATTEMPT 1 (sourceDataUrl `{sourcedataurl}`): tiers [{tiers_a}] attempted, "
        f"no usable date found. "
        f"ATTEMPT 2 (provenance_url fallback `{provenance_url}`): {recipe_b}"
    )


def recipe_no_date_fallback(sourcedataurl: str, tiers_a: str,
                             provenance_url: str, tiers_b: str) -> str:
    """Both the sourceDataUrl attempt and its provenance_url fallback were
    tried and neither found anything — the two-URL analogue of recipe_no_date()."""
    return (
        f"No verification steps — tiers [{tiers_a}] attempted against sourceDataUrl "
        f"`{sourcedataurl}` found nothing; tiers [{tiers_b}] attempted against "
        f"fallback provenance_url `{provenance_url}` also found nothing."
    )


def source_summary(tier_used, date_source: str) -> str:
    """One short, plain-English sentence naming exactly where the date came
    from — distinct from date_method (which just names the tier/category)
    and verification_steps (the full numbered trace): this is the middle
    ground for a validator scanning many rows who wants the specific
    evidence without opening the full trace or decoding a raw date_source
    code. Built purely from data already captured (tier_used + the same
    date_source string every tier already returns) — no extra Gemini call."""
    if tier_used is None or tier_used == "":
        return "N/A — no date found"
    tier_used = int(tier_used)
    ds = date_source or "unknown"

    if tier_used == 0:
        return f"Fetched directly from a domain-specific API, no page scraping — {ds}"
    if tier_used == 1:
        return f"Gemini browsed the live page and cited this as the date's location: {ds}"
    if tier_used == 2:
        return f"Downloaded the dataset file itself and read the date from it — {ds}"
    return f"Tier {tier_used} — {ds}"


def method_label(tier_used, date_source: str) -> str:
    """Short, human-scannable answer to "how was this found" — a CSV column
    version of the same tier/source info recipe_* already turns into full
    step-by-step prose."""
    if tier_used is None or tier_used == "":
        return "Not found"
    tier_used = int(tier_used)

    if tier_used == 0:
        return "Direct API / domain-specific handler"
    if tier_used == 1:
        return "Gemini computer-use (browser navigation)"
    if tier_used == 2:
        return "Downloaded file + Gemini API inspection"
    return f"Tier {tier_used}"
