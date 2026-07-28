# Provenance Refresh Date Pipeline — Changelog & Known Issues

## What the pipeline does

Extracts the **last refresh / last updated date** for each DataCommons provenance
source URL. Runs a 5-tier cascade per URL, stopping as soon as a confident date
is found:

| Tier | Method |
|------|--------|
| 1 | HTTP HEAD → `Last-Modified` header |
| 2 | GET + HTML parse → JSON-LD / Dublin Core / meta tags / body-text regex + **sub-page link following** |
| 3 | Gemini Flash NLP on rendered page text |
| 4 | Playwright (headless Chromium, `networkidle`) → same HTML extraction + Gemini |
| 5 | Groq `compound-beta` real browser (handles bot walls and JS-heavy SPAs) |

Two companion scripts:

- **`evidence_pipeline.py`** — visits `CS VALIDATION_EVIDENCE_LINK` URLs from the
  staleness spreadsheet using Playwright + Gemini to build a ground-truth date set.
- **`extract_from_evidence.py`** — earlier lighter version (no Playwright); superseded
  by `evidence_pipeline.py`.

---

## Changes made to the original pipeline

### Fix 1 — `_parse_date` rewritten with `dateutil` fuzzy parsing
**File:** `provenance_refresh_extractor.py` lines ~75–92

**Before:** Only accepted strings already in `YYYY-MM-DD` / `YYYY-MM` / `YYYY`
format. Anything written as "March 12, 2026" or buried after a label
("Last Updated: 2026-03-12") failed silently and fell through to `_first_year`.

**After:** Uses `dateutil.parser.parse(val, fuzzy=True)` — parses any
human-readable date format. Added sanity guards: year must be 2000–present+1,
future dates rejected, bare 4-digit years rejected (not precise enough).

---

### Fix 2 — Body-text match no longer truncated to bare year
**File:** `_extract_from_html`, body-text section

**Before:** `_DATE_RE` captured the full phrase (e.g. `"March 12, 2026"`) but then
the code called `_first_year()` on it, discarding the day and month and returning
only `"2026"`.

**After:** Captured phrase passed directly to `_parse_date()` — full date preserved.

---

### Fix 3 — Body-text search extended from 5,000 to 20,000 characters
**File:** `_visible_text()` helper

**Before:** `soup.get_text()[:5000]` — "Last Updated" text in page footers
(common on government sites) was never reached.

**After:** Strip `<script>`, `<style>`, `<noscript>` first, then take up to
20,000 characters.

---

### Fix 4 — `_DATE_RE` phrase list broadened
**Before:** Only matched `last updated/modified/refreshed/revised`, `data as of`,
`updated`, `as of`.

**After:** Also matches `released on`, `published on`, `data through`, `data updated`.
`effective date` deliberately **excluded** — means when a regulation takes effect,
not when the dataset was refreshed (was causing false positives on HUD income-limit
pages).

---

### Fix 5 — Page-meta false-positive filter added
**File:** `_PAGE_META_RE` regex + body-text loop

Skips body-text matches whose preceding context contains phrases like
"page last revised", "html last modified", "website last updated" — these
indicate the HTML template revision date, not the data release date.

---

### Fix 6 — Tier 1 sanity guards
**File:** `_tier1()`

**(a)** Skip entirely for `.pdf`, `.xlsx`, `.xls` — Last-Modified on binary
downloads reflects upload/generation time, not publish date.

**(b)** Reject headers less than 14 days old — dynamic page regeneration
timestamps that are not meaningful refresh events.

---

### Fix 7 — Removed `_first_year` fallback from LLM tiers (3 and 5)
**File:** `_tier3_sync()`, `_tier5_sync()`

**Before:** If `_parse_date()` failed on an LLM response, fell back to
`_first_year()` which grabbed the first 4-digit number in the string. A Gemini
response mentioning "2022 Census data" would return `"2022"` as the refresh date.

**After:** If `_parse_date()` fails, the tier returns `None` and the cascade
continues. The LLM must return a parseable date or the tier is a miss.

---

### Fix 8 — Tier 1 and Tier 2 run in parallel; Tier 2 takes priority
**File:** `process_url()` orchestrator

**Before:** Tier 1 fired first — if Last-Modified header was found, it
**immediately returned** and Tier 2 never ran. Caused wrong dates on URLs like
`aqs.epa.gov` (header said Dec 4, body text clearly said Nov 24) and
`google.com/covid19/mobility` (header Sep 2024, body "as of 2022-10-15").

**After:** Tier 1 and Tier 2 run concurrently with `asyncio.gather()`. Tier 2's
structured/body-text date wins if found. Tier 1's Last-Modified is only used
as fallback when Tier 2 **completely failed** (exception or non-HTML response) —
never when Tier 2 successfully fetched HTML but found no date.

---

### Fix 9 — Removed `article:modified_time` and `og:updated_time` from Tier 2
**File:** `_extract_from_html()`

**Before:** Pipeline treated these OpenGraph meta tags as the data refresh date.
They are CMS editorial timestamps — set when a content editor last touched the
HTML, not when the underlying dataset changed.

**Concrete failures fixed:**
- `census.gov/programs-surveys/sahie.html`: `article:modified_time = 2026-04-09`
  (page edit) vs correct data release `2025-11-25`
- `cdc.gov/places/index.html`: `og:updated_time = 2025-02-03` (page update) vs
  correct data release `2025-12-05`

---

### Fix 10 — Body-text returns most recent date, not first match
**File:** `_extract_from_html()`, body-text section

**Before:** Returned the first body-text match, which caused false positives when
historical policy notes appeared before the current refresh date. Example:
`federalreserve.gov/releases/h15/` — body text first matches
`"As of March 1, 2016, the daily effective federal funds rate..."` and returned
`2016-03-01` instead of the current 2026 update.

**After:** Collects all body-text date candidates, returns `max()`. Also applies
a 7-day recency cutoff — dates within 7 days of today are skipped as likely
dynamic "generated on" timestamps.

---

### Fix 11 — Added `cdc:last_updated` meta tag extraction
**File:** `_extract_from_html()`, meta tag section

CDC pages carry a custom `<meta name="cdc:last_updated">` tag that accurately
reflects the data content update date, separate from the generic `DC.date` which
is the CMS file-publish timestamp. Now checked **before** Dublin Core tags.

---

### Fix 12 — Improved LLM prompts (Tiers 3 and 5)
**File:** `_tier3_sync()`, `_tier5_sync()`

Added explicit instruction: *"Do NOT return the page's HTML edit date, CMS
timestamp, or publication date of a news article. Return ONLY the date when the
underlying dataset/data was last refreshed or updated."*

---

### Fix 13 — Tier 2 sub-page link following
**File:** `_tier2()`, new `_promising_sublinks()` helper

**Problem:** For program overview pages (e.g. `census.gov/programs-surveys/popest.html`),
the correct data release date lives on a linked sub-page ("March 2025 Release"),
not on the landing page itself. The pipeline was only seeing the landing page.

**Implementation:** When the main page yields no date, extract all internal links
(same domain, non-binary), score them by relevance keywords (`data`, `release`,
`download`, `latest`, `estimates`, etc.), fetch the top 6 in parallel, run date
extraction across all of them, return the most recent date found.

---

## Accuracy results (tested on 50 URLs, compared against ground-truth evidence dates)

| Version | Changes | Correct / Comparable | Accuracy | URLs with any date |
|---------|---------|----------------------|----------|--------------------|
| v1 (original) | Baseline | 8 / 23 | **34%** | 23 |
| v2 | Fixes 1–9 (core fixes) | 11 / 25 | **44%** | 25 |
| v3 | + Fix 10–12 (recency filter, cdc meta, prompts) | 9 / 23 | 39% | 23 |
| v4 | + Fix 13 (sub-page crawl) | 9 / 24 | 37% | 24 |

**v2 is the best-performing version** — the core structural fixes (parallel T1/T2,
remove CMS meta tags, most-recent body-text) gave the largest accuracy gain.
v3 and v4 introduced regressions on a small number of URLs while fixing others.

---

## Known problems and remaining issues

### Problem 1 — Program overview pages vs data release pages (structural)
**Affected URLs:** `census.gov/programs-surveys/popest.html`,
`census.gov/programs-surveys/sahie.html`, `cdc.gov/places/index.html`

**Root cause:** The provenance URL is a marketing/overview page for a data
program. The actual dataset release date lives on a linked sub-page (e.g.
`/data/datasets/time-series/demo/popest/`). The overview page only shows when
a content editor last touched the HTML ("Last Revised: Sep 25, 2025"), which
is not the data release date (Mar 6, 2025).

**Why sub-page crawling didn't fix it:** The correct sub-pages do exist but
their content is either JS-rendered or carries the same CMS "Last Revised"
timestamp problem.

**What would fix it:** A mapping of known program-page patterns to their
canonical data-release sub-pages (domain-specific knowledge), or an LLM that
can navigate multi-step to find the right page.

---

### Problem 2 — No date anywhere on the page (~15 URLs)
**Affected URLs:** `data.census.gov` table viewer, `nces.ed.gov/ccd/elsi`,
`wonder.cdc.gov`, `data.census.gov/api/access/table/download`, `eurostat`

**Root cause:** These are query interfaces or API endpoints. The "last updated"
date exists only in a backend database or API response payload — it is never
rendered into the HTML that a browser (or Playwright) sees.

**What would fix it:** Intercept the XHR/fetch API calls the page makes after
load using Playwright's `page.route()` or `page.on("response")`, parse the
JSON payloads for date fields.

---

### Problem 3 — JS-rendered SPAs with lazy-loaded dates (~10 URLs)
**Affected URLs:** OECD data explorer, eurostat databrowser, `kosis.kr`,
`data-explorer.oecd.org`, statcan interactive tables

**Root cause:** The date is rendered inside a React/Vue/Angular component that
loads via a subsequent API call after the initial page load. Even with
`networkidle` + 3.5s settle, Playwright captures the shell HTML but the date
widget hasn't mounted yet.

**What would fix it:** Explicit `page.wait_for_selector()` targeting the specific
date element, or API call interception.

---

### Problem 4 — `DC.date` is CMS publish timestamp, not data date
**Affected URLs:** `cdc.gov/nchs/nvss/vsrr/provisional-tables.htm`,
`cdc.gov/places/index.html`

**Root cause:** CDC's Drupal CMS sets `<meta name="DC.date">` to the date the
HTML file was last published to the server, not the date the underlying health
data was updated. `cdc:last_updated` is the correct field, but on some CDC pages
it's absent or also incorrect.

**Partial fix applied:** Now check `cdc:last_updated` before `DC.date`. But when
a JSON-LD `dateModified` is also present (also a CMS timestamp), it fires first
and overrides everything.

**Remaining issue:** JSON-LD `dateModified` on CMS-heavy sites (Drupal, WordPress)
reflects editorial changes, not data changes. Currently no way to distinguish
without site-specific knowledge.

---

### Problem 5 — LLM picking wrong date when multiple dates on page (~5 URLs)
**Affected URLs:** `health.ny.gov`, `statcan.ca`, `stats.govt.nz`, `rbi.org.in`

**Root cause:** Page contains multiple plausible dates (report publication date,
data collection period, data release date, page revision date). Gemini and Groq
consistently pick the wrong one despite explicit prompting.

**Partial fix applied:** Prompt now explicitly says "Do NOT return the HTML edit
date or CMS timestamp". Still insufficient for pages with 3+ competing dates.

**What would fix it:** Multi-candidate voting (run LLM 3× and take majority),
or chain-of-thought prompting asking the LLM to list all dates found and then
select the most relevant one.

---

### Problem 6 — Daily-updated pages with dynamic "today" timestamps
**Affected URLs:** `federalreserve.gov/releases/h15/`

**Root cause:** The H15 rate release updates daily. The page body says
"data as of [today's date]" — a live pointer to current data, not a meaningful
refresh event. After the 7-day recency filter was added, the pipeline falls
back to the only other body-text date: the 2016 historical policy note.

**No clean fix:** The concept of "last refresh date" doesn't apply to daily
live feeds. The correct answer (the evidence date `2026-05-10`) represents
when the *series methodology* was last updated, which is not expressed anywhere
on the page.

---

### Problem 7 — `data.cityofnewyork.us/browse` returns wrong dataset date
**Affected URLs:** `data.cityofnewyork.us/browse?category=Education`

**Root cause:** This is a browse/catalog page listing hundreds of datasets,
each with its own modification date. The pipeline picks up one of those dates
(varies by run) rather than the correct date for the specific dataset in question.

**What would fix it:** The provenance URL needs to point to the specific dataset
page, not the category browse page.

---

## Files in this package

| File | Purpose |
|------|---------|
| `provenance_refresh_extractor.py` | Main 5-tier pipeline — run on `Provenance.csv` |
| `evidence_pipeline.py` | Playwright + Groq pipeline for evidence validation links |
| `extract_from_evidence.py` | Lightweight earlier version of evidence pipeline |
| `Provenance.csv` | Input: 686 DataCommons provenance URLs |
| `provenance_refresh_dates.json` | Output: extracted dates keyed by dataset ID |
| `provenance_evidence_dates.xlsx` | Ground-truth dates from evidence link pipeline |
| `provenance_top50_v2_results.json` | Best test run results (44% accuracy on 50 URLs) |

## How to run

```bash
# Full run on all 686 URLs (all 5 tiers)
python3 provenance_refresh_extractor.py

# Resume a partial run
python3 provenance_refresh_extractor.py --resume

# Limit to tiers 1–2 only (fast, no LLM/browser)
python3 provenance_refresh_extractor.py --tier-max 2

# Run on a specific CSV with custom output
python3 provenance_refresh_extractor.py --csv my_urls.csv --output my_results.json

# Evidence link pipeline (visits CS VALIDATION_EVIDENCE_LINK column)
python3 evidence_pipeline.py
```

## Dependencies

```
aiohttp
beautifulsoup4
python-dateutil
openpyxl
playwright
google-generativeai
groq
python-dotenv
```

## Environment variables (`.env`)

```
GEMINI_API_KEY=...
GROQ_API_KEY=...
```
