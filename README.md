# Provenance Refresh-Date Pipeline

Given a list of dataset source URLs, this pipeline finds the **last refresh /
last updated date** for each one — the date the underlying data (not the
webpage) was last changed. It runs a 6-step cascade per URL (cheapest and most
reliable methods first, LLMs and real browsers only as a last resort), plus a
set of domain-specific fixes for sources that a generic scraper can never
handle correctly.

---

## 1. Setup

### 1a. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

`playwright install chromium` is required once — it downloads the actual
headless browser binary Tier 4 launches; installing the `playwright` Python
package alone is not enough.

### 1b. Configure API keys

Copy `.env.example` to `.env` and fill in real values:

```bash
cp .env.example .env
```

```
GROQ_API_KEY=...       # required — powers Tier 3 (text reasoning) and Tier 5 (real-browser agent)
GITHUB_TOKEN=...       # required for the GitHub-tree-page handler (Commits API)
GEMINI_API_KEY=...     # NOT currently used — see "Why no Gemini" below
```

**Why no Gemini:** the pipeline originally used Gemini for Tier 3. The
`GEMINI_API_KEY` was found to be invalid (`API_KEY_INVALID` from Google's
API), meaning Tier 3 had been silently contributing nothing — the failure
was swallowed by a bare `except Exception: pass` and never surfaced. Rather
than fix the credential (an account/billing action outside this codebase),
Tier 3 and Tier 4's fallback were re-pointed at Groq (`openai/gpt-oss-120b`),
which was already confirmed working via Tier 5. If a valid Gemini key is
restored later, it would be worth re-testing Gemini against Groq head-to-head
before switching back — model choice was only validated against Groq's own
lineup.

---

## 2. Input format

A CSV with exactly these three columns (extra columns are ignored):

```csv
id,prov_id,provenance_url
dc/base/MSTEP_3-8Grades,dc/base/MSTEP_3-8Grades,https://www.mischooldata.org
dc/base/INPE_Fire_Event_Count,dc/base/INPE_Fire_Event_Count,https://terrabrasilis.dpi.inpe.br/queimadas/situacao-atual/estatisticas/estatisticas_estados/
```

- `id` — the dataset identifier this row belongs to (used as the key in the output JSON).
- `prov_id` — currently unused by the pipeline itself, carried through from the source data.
- `provenance_url` — the URL to fetch. **Can contain multiple comma-separated URLs** in one cell (e.g. `https://a.com, https://b.com`) — these are automatically split into separate lookups, each attributed back to the same `id`.

The default input file is `Provenance.csv` in this directory (686 rows as
shipped). Point `--csv` at any other file with the same three columns to run
on a different set.

---

## 3. How to run

```bash
# Full run on all rows in Provenance.csv, all tiers, default output file
python3 provenance_refresh_extractor.py

# Run on a specific CSV, write to a specific output file
python3 provenance_refresh_extractor.py --csv my_urls.csv --output my_results.json

# Resume a previous run — skips any dataset ID that already has a
# last_refresh_date in the existing output file, only retries misses
python3 provenance_refresh_extractor.py --resume

# Cap how deep the cascade goes (useful for a fast, free, no-LLM dry run)
python3 provenance_refresh_extractor.py --tier-max 2   # only Tier 0 (domain handlers) + Tier 1 + Tier 2, no Groq/Playwright
python3 provenance_refresh_extractor.py --tier-max 4   # everything except Tier 5 (compound-beta real browser)
```

### CLI flags

| Flag | Default | Meaning |
|---|---|---|
| `--csv` | `Provenance.csv` | Input CSV path |
| `--output` | `provenance_refresh_dates.json` | Output JSON path |
| `--tier-max` | `5` (or `$TIER_MAX` env var) | Highest numbered tier (1–5) allowed to run. Tier 0 (domain handlers) and the final Census fallback always run regardless of this setting. |
| `--resume` | off | Skip dataset IDs that already have a date in `--output`'s existing file |

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `GROQ_API_KEY` | — | required |
| `GITHUB_TOKEN` | — | required for the GitHub handler |
| `TIER_MAX` | `5` | same as `--tier-max`, as an env var fallback |
| `GROQ_TEXT_CONCURRENCY` | `8` | max concurrent Groq calls for Tier 3's `openai/gpt-oss-120b` (text reasoning) |

Two concurrency settings are hardcoded in the file (`CONCURRENCY = 20` global
parallel URLs, `DOMAIN_LIMIT = 2` max concurrent requests per domain,
`GROQ_CONCURRENCY = 3` for Tier 5's `compound-beta`) — edit the constants near
the top of `provenance_refresh_extractor.py` directly if you need to change
these.

### Validating results against ground truth

```bash
python3 validate.py --results provenance_refresh_dates.json --ground-truth provenance_evidence_dates.xlsx
python3 validate.py --results provenance_refresh_dates.json --ground-truth provenance_evidence_dates.xlsx --verbose
```

`--verbose` additionally lists every URL in the `mismatch`, `miss`, and
`false_pos` buckets so you can see exactly what went wrong, not just the
counts.

`provenance_evidence_dates.xlsx` must have a sheet (named `Evidence Dates`,
or just the first sheet) with columns `provenance_url`, `evidence_url`,
`last_refresh_date`, `date_source`. A URL can appear more than once (a
landing page can cover several underlying datasets, each with its own
verified date) — a pipeline result counts as a match if it equals *any* of
the ground-truth dates recorded for that URL.

---

## 4. Output format

Written to the `--output` path (default `provenance_refresh_dates.json`), keyed by dataset `id`:

```json
{
  "dc/base/BLS_CES_State": {
    "url": "https://www.bls.gov/sae/overview.htm",
    "last_refresh_date": "2026-06-10",
    "date_source": "body-text",
    "tier_used": 2,
    "date_found": true,
    "tiers_attempted": "1,2",
    "tier_failed_reason": null,
    "extraction_time_sec": 0.84
  }
}
```

| Field | Meaning |
|---|---|
| `url` | the (possibly normalized) URL actually fetched |
| `last_refresh_date` | `YYYY-MM-DD`, or `null` if nothing was found |
| `date_source` | where the date came from — a tag like `json-ld:dateModified`, `body-text`, `github-commits-api:committer`, `Last-Modified header (via https://query.wikidata.org/)`, etc. Always human-readable enough to audit *why* this date was chosen. |
| `tier_used` | which tier produced the final answer (`0` = a domain-specific handler or the last-resort Census fallback, `1`–`5` as described below), or `null` if nothing was found |
| `date_found` | boolean shortcut for `last_refresh_date is not null` |
| `tiers_attempted` | comma-separated list of every tier that was actually tried for this URL, in order, e.g. `"1,2,3,4,5,0"` |
| `tier_failed_reason` | human-readable reason if nothing was found, or if the URL was skipped entirely (catalog page, not externally trackable, etc.) |
| `extraction_time_sec` | wall-clock time spent on this URL |

A second file, `<output>_catalog_report.json`, is also written listing any
URLs that were recognized as browse/category/search pages (e.g.
`...?category=Education`) and skipped entirely — these have no single
correct date to extract in the first place.

---

## 5. How it works — step by step, per URL

For a single URL, the pipeline tries the following in order and **stops at
the first success**:

### Step 0 — Structural skip checks (no network call)
- Catalog/browse/search-pattern URLs (`category=`, `/browse`, `/search` in the path) are rejected outright — a listing page has no single correct date.
- `datacommons.org` URLs are flagged "not externally trackable" and rejected — this is a self-referential source with no external date to find at all.

### Step 1 — Domain-specific handlers ("Tier 0")
Six URL patterns are checked against known problem sources, each calling that
source's own authoritative API directly (no LLM, no browser, at most one
HTTP request):

| Source | How the date is found |
|---|---|
| Wikidata homepage | Redirects to `query.wikidata.org` and reuses Tier 1's HEAD-request logic there instead |
| `nccs.nasa.gov` (NASA NEX products) | NASA Earthdata CMR API, `meta.revision-date` field |
| `data.humdata.org/dataset/*` | HDX's CKAN `package_show` API, `metadata_modified` field |
| `ndap.niti.gov.in/dataset/*` | NDAP's snippet API |
| `github.com/*/tree/*` | GitHub Commits API — exact last-commit date for that path |
| `gaftp.epa.gov/*` | Parses the raw FTP-style directory listing for file timestamps |

If a pattern matches but that source's API call itself fails (e.g. the API
is down), the pipeline falls through to Step 2 exactly as if this step
didn't exist — it can only help, never hurt.

*(A seventh handler, the Census `tid=ACSST5Y{year}` URL-embedded vintage
year, deliberately does **not** run here — see Step 6.)*

### Step 2 — Tier 1 + Tier 2, run concurrently
- **Tier 1**: HTTP `HEAD` request, checks the `Last-Modified` response header. Discarded if less than 14 days old (likely just dynamic page regeneration, not a real content change).
- **Tier 2**: full `GET`, then searches the HTML for, in priority order: JSON-LD structured metadata → site-specific meta tags (e.g. CDC's `cdc:last_updated`) → Dublin Core meta tags → a regex scan of visible body text for phrases like "last updated," "data as of," "released on." If the main page has nothing, it automatically follows the top 6 same-domain links that look most likely to be a data-release sub-page (scored by keyword relevance) and takes the most recent date found across all of them.

Tier 2's result wins if found. Tier 1's `Last-Modified` header is used only
as a fallback when Tier 2 completely failed (network error, or a non-HTML
response) — never as a tiebreaker when Tier 2 loaded the page fine but just
didn't find a date, since a working page without visible metadata doesn't
mean the CDN's cache-header timestamp is meaningful.

### Step 3 — Tier 3: Groq LLM reasoning over the page text
Only runs if Tier 2 got HTML back. Sends the already-fetched text to
`openai/gpt-oss-120b` with a prompt that explicitly lists five decoy
categories to reject — copyright/footer years, CMS "page last reviewed"
timestamps, unrelated news article dates, future "next release" dates, and
unrelated historical footnotes — and requires the model to quote the exact
phrase it used, so a wrong pick is auditable via `date_source` rather than a
bare unexplained date.

### Step 4 — Tier 4: Playwright real browser render
For JS-heavy pages that return an empty HTML shell to a plain `GET`. Launches
headless Chromium, waits for the DOM plus a 2-second settle, then reruns the
exact same HTML-extraction logic from Step 2 on the rendered page. If that
still finds nothing, it falls back to the same Groq text-reasoning call as
Step 3, this time on the rendered text.

### Step 5 — Tier 5: Groq `compound-beta` real-browsing agent
The last real attempt, and mechanically different from every step above:
instead of *this pipeline* fetching a page and handing text to a model, this
Groq model visits the URL itself with its own built-in browsing tool. Used
for bot-walled or heavily obstructed sites where even Playwright fails.

### Step 6 — Absolute last resort
If every tier above found nothing, and the URL matches a Census
`tid=ACSST5Y{year}` pattern, the vintage year is read directly out of the URL
string. This runs dead last, deliberately — it's a coarse year-only guess,
not a real refresh signal, so it must never override a more precise date any
earlier tier already found. (Earlier in development this ran *before* Tiers
1–2 and caused a real regression — it overwrote some already-correct,
precise dates with this coarser guess. Moving it to last-resort fixed that.)

---

## 6. Rate limiting / concurrency model

- `CONCURRENCY = 20` — up to 20 URLs processed in parallel overall.
- `DOMAIN_LIMIT = 2` — no more than 2 concurrent requests to the same domain at once, regardless of how many URLs on that domain are queued.
- `GROQ_CONCURRENCY = 3` — Tier 5's `compound-beta` (a real-browsing agent with a much tighter rate limit, ~30 requests/min).
- `GROQ_TEXT_CONCURRENCY = 8` (env-overridable) — Tier 3/4's `openai/gpt-oss-120b` text calls, kept separate from the above since this is a plain-inference model with much higher usable throughput, and fires far more often than Tier 5.

---

## 7. Known limitations (not fixed by this pipeline)

- **Wrong page, not wrong date.** If the provenance URL is a homepage or overview page and the actual dataset page is elsewhere on the site, every tier will confidently return the most defensible date *on the page it was given* — which may not be the right page at all. Fixing this needs a navigation/multi-hop capability that does not exist yet.
- **Daily/rolling data feeds.** Some sources (e.g. Federal Reserve H.15) update daily; the pipeline correctly finds the page's real current release marker, but some definitions of "last refresh" actually want a different concept (e.g. "when the methodology last changed") that isn't rendered anywhere on the page at all.
- **`GEMINI_API_KEY` is broken, not fixed.** Groq is a working substitute for the LLM-reasoning steps, not a permanent resolution to the credential itself.
- See `PIPELINE_CHANGELOG.md` and `REFRESH_DATE_BLOCKERS.md` for the full history of fixes attempted, what regressed, and a per-domain breakdown of structurally-blocked sources not yet covered by a Tier 0 handler.

---

## 8. File manifest

| File | Purpose |
|---|---|
| `provenance_refresh_extractor.py` | Main pipeline — the 6-step cascade described above |
| `specialized_source_handlers.py` | Tier 0 domain-specific handlers, URL normalization, blocker classification |
| `validate.py` | Scores a results JSON against a ground-truth Excel |
| `Provenance.csv` | Default input (686 provenance URLs) |
| `provenance_evidence_dates.xlsx` | Ground truth used by `validate.py` |
| `PIPELINE_CHANGELOG.md` | Full history of fixes made to the original pipeline, and what each one addressed |
| `REFRESH_DATE_BLOCKERS.md` | Per-domain breakdown of the 258 structurally-blocked URLs found in the full 686-URL corpus, and the fix path identified for each |
| `.env.example` | Template for required API keys — copy to `.env` and fill in real values |
