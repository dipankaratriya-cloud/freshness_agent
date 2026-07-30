"""
Tier 6 prompt — kept in its own file (separate from computer_use_extractor.py)
so the instructions given to the model can be read/tuned without touching
the agent loop / Playwright execution code.
"""

TASK_PROMPT_TEMPLATE = """You are controlling a live browser tab currently open to this URL:
{url}

Find the LAST REFRESH / LAST UPDATED / DATA AS OF date for the DATASET on this page.

This page may show several dates that are NOT the answer. Do NOT return:
  - a copyright or footer year (e.g. "© 2026")
  - a CMS editorial timestamp ("page last reviewed", "page last revised" — referring to
    when the HTML/page itself was edited, not the underlying data)
  - an unrelated news/article publish date
  - a future "next release" / "upcoming update" date
  - a historical footnote or per-row date that isn't the overall refresh date
  - the END of an "availability" / "coverage" / "as of" date RANGE that lands on today or
    the last few days — that end value is usually generated live at page-load time to mean
    "up through right now," not a record of when the data was actually refreshed

BEFORE EACH ACTION, briefly reason (in your own thinking, not in the final output):
what have I learned from the screenshot so far, what is the single most promising place
left to check, and why. Don't click around at random — treat this like an investigation
where each step should be justified by something you actually observed.

WHERE TO LOOK, roughly in priority order (skip any that don't apply to this site):
  1. The current page itself — header, footer, sidebar, a table caption, or an
     "About this data" / "Metadata" / "Data dictionary" section.
  2. If the URL looks like an API endpoint (returns JSON/XML rather than a rendered
     page), read the raw response text directly — API metadata responses very often
     carry a machine-readable field like "lastupdated", "modified", "updated_at", or
     a <lastupdated> / <dateModified> element. This is frequently the most reliable
     source, more reliable than anything on a rendered HTML page.
  3. A linked "Data"/"Download"/"Release notes"/"Changelog"/"Version history" page on
     the same site — these often state the refresh date explicitly and are worth one
     navigation if the current page doesn't have it.
  4. The site's main data catalog or API documentation, if this URL is a deep link
     that doesn't itself carry metadata — search or navigate to find the catalog entry
     for this specific dataset.
  5. A cookie/consent banner blocking the view — dismiss it (accept/close) as your
     first action if one is visible, then proceed with the above.

Prefer exploring 2-3 of the most promising places over exhaustively re-checking the
same page repeatedly. If an action doesn't change what's visible (e.g. a click did
nothing), don't repeat the identical action — try a different element or approach.

You have at most {max_steps} actions total — use them purposefully rather than
running out the clock on unpromising paths.

When you are confident you have found the answer, OR you have exhausted reasonable
places to look, STOP calling any action and instead reply with plain text containing
ONLY this JSON object (no markdown fencing, no explanation before or after it):
{{"date": "YYYY-MM-DD or null", "source": "exact text or element where you found it"}}
"""
