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
  - a CMS editorial timestamp — "page last reviewed", "page last revised", OR a
    site/homepage-wide "Site last updated" / "Website last updated" footer stamp.
    All of these describe when the HTML/page/site was last touched, not when the
    underlying dataset's content changed — reject this whole category even when the
    exact wording differs from these examples.
  - an unrelated news/article publish date
  - a future "next release" / "upcoming update" date
  - a historical footnote or per-row date that isn't the overall refresh date
  - the END of an "availability" / "coverage" / "as of" date RANGE that lands on today or
    the last few days — that end value is usually generated live at page-load time to mean
    "up through right now," not a record of when the data was actually refreshed
  - a generic API "updated" / "modified" / "lastupdated" JSON field that turns out to be a
    PLATFORM-LEVEL timestamp shared across many different datasets on the same system,
    rather than specific to the one dataset you're checking — see the API-metadata warning
    below, this is a common and easy mistake to make because it looks precise and
    machine-readable.

BEFORE EACH ACTION, briefly reason (in your own thinking, not in the final output):
what have I learned from the screenshot so far, what is the single most promising place
left to check, and why. Don't click around at random — treat this like an investigation
where each step should be justified by something you actually observed.

IMPORTANT — API/JSON metadata is NOT automatically more trustworthy than the rendered
page. A machine-readable "updated"/"modified"/"lastupdated" field is tempting to trust
just because it looks precise, but on several major statistical platforms (Eurostat,
OECD, World Bank, Census API) that field has been observed to report when the whole
API/database was last synced or re-indexed — a platform-wide timestamp identical across
many unrelated datasets — not when this specific dataset's content last changed. A
strong tell that you've hit this trap: the value doesn't move at all if you check it for
a different, unrelated dataset on the same platform. Whenever a page has BOTH a
dedicated, dataset-specific "Last updated" panel in the rendered UI AND a generic
API/JSON "updated" field, PREFER the UI panel's per-dataset value — it has proven far
more reliable in practice.

WHERE TO LOOK, roughly in priority order (skip any that don't apply to this site):
  1. The current page itself — header, footer, sidebar, a table caption, or an
     "About this data" / "Metadata" / "Data dictionary" / "Dataset information" section.
     This dataset-specific panel (when the site has one) is usually the single best
     source — prefer it over a generic API field (see warning above).
  2. A linked "Data"/"Download"/"Release notes"/"Changelog"/"Version history" page on
     the same site — these often state the refresh date explicitly and are worth one
     navigation if the current page doesn't have it.
  3. The site's main data catalog (e.g. catalog.data.gov, or the platform's own search),
     if this URL is a deep link that doesn't itself carry metadata — search or navigate
     to find the catalog entry for this specific dataset.
  4. If the URL looks like an API endpoint (returns JSON/XML) and nothing dataset-specific
     turned up elsewhere, read the raw response text for a field like "lastupdated",
     "modified", or a <dateModified> element — but sanity-check it against the platform
     warning above before trusting it as the final answer.
  5. If the direct URL is blocked (bot-wall / "Access Denied") and no other path works,
     check the Wayback Machine (web.archive.org) for an archived snapshot of the same
     page or, for a raw file server, its directory listing — these often show a genuine
     Last-Modified-style timestamp per file (format like "7/2/2026  8:30 AM   <size>
     <filename>" — that's M/D/YYYY, not D/M). IMPORTANT CAVEAT: an archived snapshot can
     itself be stale — it only proves the file was already updated by the snapshot's own
     capture date, not that nothing changed since. If the snapshot you land on is more
     than a few weeks old, actively look for a more recent capture (try a later date in
     the Wayback URL, or the calendar view) rather than accepting the first one you find.
  6. A cookie/consent banner blocking the view — dismiss it (accept/close) as your
     first action if one is visible, then proceed with the above.

DATE FORMAT: watch for day-first formats on non-US sites — e.g. Eurostat labels this
exact field "Last data update: DD/MM/YYYY HH:MM" (11/06/2026 means 11 June, not
November 6). Convert carefully; don't assume MM/DD just because that's the US default.

NO EXPLICIT "LAST UPDATED" LABEL ANYWHERE — DO NOT GIVE UP, INFER IT FROM THE DATA ITSELF:
most real datasets never say "last refresh date" in those words. Two common shapes,
both answerable from what you can actually see on screen:

  - WIDE-FORMAT TABLES: a table with YEARS (or year-quarters/year-months) as the
    COLUMN HEADERS across the top (e.g. columns "2020 | 2021 | 2022 | 2023", each
    holding that period's row of values), with no separate "updated" text anywhere.
    Here, the answer is simply the MOST RECENT period that has an actual populated
    column — not a labeled date, the column header itself. Scroll/check the full
    width of the table before concluding which column is last, since wide tables
    often extend past the visible viewport.
  - RAW DATA FILES (CSV/TSV/TXT) instead of a rendered page: if navigating to the URL
    shows the file's own content as plain text in the tab (not a download prompt),
    treat it the same way — the refresh date is often the MOST RECENT date/year
    appearing in the data itself (e.g. the last row of a time series). Scroll toward
    the end of the visible content to check, but cap this at a few scrolls; if the
    file is too large to reasonably reach the end within your remaining action
    budget, say so in your reasoning and prefer any other source you already found
    over guessing at an unverified value.

CAPABILITY LIMIT — you cannot open files that download to disk: if navigating to a
URL triggers an actual file download (rather than rendering as text in the tab), you
have no way to view that file's contents — there is no action available to open a
downloaded file. Do not click through OS download dialogs or repeat the download
hoping for a different result; that wastes actions on something that cannot work.
Instead, look for a directory listing (see the Wayback Machine guidance above), a
separate documentation/metadata page for the same dataset, or return null rather
than guessing.

Prefer exploring 2-3 of the most promising places over exhaustively re-checking the
same page repeatedly. If an action doesn't change what's visible (e.g. a click did
nothing), don't repeat the identical action — try a different element or approach. In
particular, don't scroll the same direction more than twice in a row without new
information appearing — if two scrolls in a row reveal nothing new, that section is
exhausted; move to a different page or approach instead of scrolling further.

You have at most {max_steps} actions total — use them purposefully rather than
running out the clock on unpromising paths.

When you are confident you have found the answer, OR you have exhausted reasonable
places to look, STOP calling any action and instead reply with plain text containing
ONLY this JSON object (no markdown fencing, no explanation before or after it):
{{"date": "YYYY-MM-DD or null", "source": "exact text or element where you found it"}}
"""
