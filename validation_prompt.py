"""
Confidence-validation prompt — a second, independent Gemini check on dates
found by the cheap deterministic body-text regex (provenance_refresh_extractor.
_DATE_RE), which has no way to tell a genuine refresh notice apart from an
unrelated decoy (news item, paper-publication announcement, etc. that just
happens to sit near a trigger word). Structured hits (JSON-LD, meta tags,
API fields) are NOT run through this — those are already reliable,
machine-readable signals and don't need a second opinion.
"""

VALIDATION_PROMPT_TEMPLATE = """A pattern-matching script scanned this page and found a candidate
LAST REFRESH / LAST UPDATED date for the dataset on it, based only on a nearby trigger word
(e.g. "updated", "as of", "data released/published") — it has no real understanding of context,
so it can easily be fooled by a date that's actually about something else entirely.

URL: {url}
Candidate date the script extracted: {candidate_date}

Full page text (visible text only, up to 500000 chars):
{page_text}

Your job: decide whether {candidate_date} is GENUINELY the date the underlying DATASET/DATA was
last refreshed or updated, or whether the script was fooled by one of these decoys:
  - a copyright or footer year
  - a CMS editorial timestamp ("page last reviewed/revised" — the HTML/page itself, not the data)
  - an unrelated news/article/blog-post date, or an announcement that a paper or report was
    published/released (this is a common false positive — publishing a PAPER about a dataset is
    not the same as refreshing the dataset)
  - a future "next release" / "upcoming update" date
  - a historical footnote or per-row date that isn't the overall refresh date
  - the END of an "availability"/"coverage"/"as of" date RANGE that lands on today or the last
    few days (usually generated live at page-load time, not a real refresh record)

Only answer confident=true if you can find clear textual support, elsewhere on this page, that
{candidate_date} specifically describes when the DATASET's underlying data was refreshed — not
merely that the date appears somewhere on the page.

Return ONLY valid JSON, no markdown, no explanation:
{{"confident": true or false, "reason": "one sentence explaining what the date actually refers to"}}
"""
