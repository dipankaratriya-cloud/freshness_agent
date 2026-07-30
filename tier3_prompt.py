"""
Tier 3 prompt — Gemini 3.1 Pro text-reasoning fallback.

Kept in its own file (same convention as computer_use_prompt.py) so the
decoy-exclusion rules can be read/tuned without touching the tier-cascade
code. This is the Gemini replacement for the old Groq openai/gpt-oss-120b
prompt in provenance_refresh_extractor.py._tier3_sync — same decoy-aware
rules, plus the "as of now" range-end warning added after Tier 6 testing
surfaced that exact false-positive pattern (see computer_use_prompt.py).
"""

TIER3_PROMPT_TEMPLATE = """URL: {url}
Page text (visible text only, up to 20000 chars):
{page_text}

What is the LAST REFRESH / LAST UPDATED date for the DATASET or DATA on this page?

This page likely contains several dates that are NOT the answer. Do NOT return:
  - a copyright or footer year (e.g. "© 2026")
  - a CMS editorial timestamp ("page last reviewed", "page last revised" — referring
    to the HTML/page itself, not the data)
  - an unrelated news/article publish date
  - a future "next release" / "upcoming update" date
  - a historical footnote or per-row date elsewhere on the page that isn't the overall refresh date
  - the END of an "availability" / "coverage" / "as of" date RANGE that lands on today or
    the last few days — that end value is usually generated live at page-load time to mean
    "up through right now," not a record of when the data was actually refreshed

Return ONLY the date the underlying DATASET/DATA was actually last refreshed or updated.
If no such date is genuinely present, return null rather than guessing.
Return ONLY valid JSON, no markdown, no explanation:
{{"date": "YYYY-MM-DD or null", "source": "exact quoted phrase from the page text you used"}}
"""
