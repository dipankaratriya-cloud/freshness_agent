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

You may click, scroll, dismiss cookie/consent banners, wait for content to load, or
navigate to a linked data/download/release sub-page on the same site if that helps you
find the real answer. You have at most {max_steps} actions total, so prioritize the most
likely places to look (page header/footer, "About this data", download/release pages)
before exploring further.

When you are confident you have found the answer, OR you have exhausted reasonable
places to look, STOP calling any action and instead reply with plain text containing
ONLY this JSON object (no markdown fencing, no explanation before or after it):
{{"date": "YYYY-MM-DD or null", "source": "exact text or element where you found it"}}
"""
