"""
Provenance URL → Last Refresh Date extractor.

5-tier pipeline per URL:
  Tier 1 : HTTP HEAD  → Last-Modified header
  Tier 2 : GET + HTML → JSON-LD / OpenGraph / Dublin-Core / regex
  Tier 3 : Groq (openai/gpt-oss-120b) on page text (NLP fallback, decoy-aware)
  Tier 4 : Playwright full render (JS-heavy sites)
  Tier 5 : Groq compound-beta real browser (bot-blocked / JS-wall sites)

Run:
  python3 provenance_refresh_extractor.py
  python3 provenance_refresh_extractor.py --resume       # retry misses from last run
  python3 provenance_refresh_extractor.py --tier-max 2   # skip Gemini + Playwright + Groq
"""

import asyncio
import json
import logging
import os
import re
import sys
import csv
import time as _time
from collections import defaultdict
from datetime import date as _date, datetime
from urllib.parse import urlparse

from dateutil import parser as _dateparser
from dateutil.parser import ParserError as _ParserError

import aiohttp
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from specialized_source_handlers import (
    normalize_url,
    classify_blocker,
    handle_census_url_vintage,
    SPECIALIZED_HANDLERS,
)

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from groq import Groq as _Groq
_groq_client = _Groq(api_key=os.environ.get("GROQ_API_KEY", ""))

# ── logging ───────────────────────────────────────────────────────────────────
# Every tier previously swallowed its own exceptions with a bare `except: pass`,
# so a broken credential or a rate-limit block looked identical to "genuinely
# no date found" — this is exactly how the invalid GEMINI_API_KEY went
# unnoticed for an unknown period. logger.debug() calls below make the real
# reason visible in the per-run log file without changing any tier's
# fall-through behavior (a failure still means "try the next tier").
logger = logging.getLogger("refresh_pipeline")

LOGS_DIR = os.path.join(os.path.dirname(__file__), "logs")


def setup_logging(log_path: str | None = None) -> str:
    """Configures `logger` to write DEBUG-level detail to a per-run file
    (every fetch, every tier decision, every LLM prompt/response, every
    caught exception with its real type+message) while keeping the existing
    console print() output as the at-a-glance progress view. Returns the
    resolved log file path."""
    os.makedirs(LOGS_DIR, exist_ok=True)
    if log_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = os.path.join(LOGS_DIR, f"run_{ts}.log")

    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()  # avoid duplicate handlers if setup_logging is called twice in one process

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)
    return log_path


# ── config ────────────────────────────────────────────────────────────────────

INPUT_CSV    = os.path.join(os.path.dirname(__file__), "Provenance.csv")
OUTPUT_JSON  = os.path.join(os.path.dirname(__file__), "provenance_refresh_dates.json")
# overridden by --csv / --output CLI args
TIER_MAX     = int(os.environ.get("TIER_MAX", "5"))
GROQ_CONCURRENCY = 3   # compound-beta rate limit is ~30 RPM; 3 concurrent is safe
# Separate, more generous limit for plain-inference Groq calls (Tier 3's
# openai/gpt-oss-120b) — it fires on every URL that reaches Tier 3 (far more
# often than Tier 5's compound-beta) and has much higher usable throughput
# than an agentic browsing model, so it must not share _groq_sem.
GROQ_TEXT_CONCURRENCY = int(os.environ.get("GROQ_TEXT_CONCURRENCY", "8"))
CONCURRENCY  = 20          # global async workers
DOMAIN_LIMIT = 2           # max concurrent requests per domain
REQUEST_TO   = 10          # HTTP timeout (seconds)
HEADERS      = {"User-Agent": "Mozilla/5.0 (compatible; StalenesBot/1.0)"}

_DATE_RE = re.compile(
    r'\b(?:'
    r'last\s+(?:updated?|modified|refreshed?|revised?)|'
    r'data\s+(?:as\s+of|updated?|through)|'
    r'updated?|as\s+of|'
    # Bare "released"/"published" (no "data"/"dataset" qualifier) is too weak a
    # trigger — it matches unrelated news/article-publication blurbs (e.g. "our
    # paper is published at Joule") just as readily as a real refresh notice.
    # Requiring the qualifier keeps legitimate "Data released: <date>" /
    # "Dataset published <date>" phrasing while dropping that decoy pattern.
    r'data(?:set)?\s+released?(?:\s+on)?|data(?:set)?\s+published?(?:\s+on)?'
    # "effective date" deliberately excluded — means when a regulation/policy
    # takes effect, not when the underlying dataset was refreshed
    r')[:\s]+([A-Za-z0-9,\s/-]{4,30})',
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r'\b(20[012]\d|19\d{2})\b')
# Phrases that indicate HTML/CMS revision time, not data release time
_PAGE_META_RE = re.compile(
    r'(?:page|html|web(?:site)?|last)\s+(?:last\s+)?(?:revised|updated|modified)',
    re.IGNORECASE,
)
_BODY_DATE_RECENCY_DAYS = 7   # body-text dates within this many days are likely dynamic "as of today"
_SKIP_LASTMOD_EXTS = {".pdf", ".xlsx", ".xls"}
_TODAY = _date.today().isoformat()

# ── helpers ───────────────────────────────────────────────────────────────────

# Catalog/browse/search pages list many datasets at once (each with its own
# date) rather than a single dataset — there is no single correct date to
# extract from the URL itself. Narrow on purpose (see Task 10): expand only
# once real hits against the full 686-URL set justify it.
_CATALOG_URL_PATTERNS = ("category=", "/browse", "/search")


def classify_url(url: str) -> str:
    """Returns "catalog" for browse/search/category listing pages, else "dataset"."""
    parsed = urlparse(url)
    haystack = f"{parsed.path}?{parsed.query}".lower()
    if any(p in haystack for p in _CATALOG_URL_PATTERNS):
        return "catalog"
    return "dataset"


# Continuously-updating (daily/business-day) feeds: the recency guard's premise
# ("a date within N days of today is probably a dynamic 'as of today' pointer,
# not a meaningful refresh") is backwards here — for these sources a fresh date
# IS the correct signal. Kept to an explicit allowlist per Task 3: a generic
# body-text detector risks false positives across the full 686-URL set until
# calibrated against more real examples than just Fed H.15.
_DAILY_CADENCE_URL_ALLOWLIST = (
    "federalreserve.gov/releases/h15",
    "federalreserve.gov/datadownload/choose.aspx?rel=h15",
)
_DAILY_CADENCE_PHRASE_RE = re.compile(
    r'\(daily\)|updated\s+daily|updated\s+every\s+business\s+day|'
    r'updated\s+each\s+business\s+day',
    re.IGNORECASE,
)
# The title-line pattern on allowlisted pages, e.g.
# "H.15 - Selected Interest Rates (Daily) - July 02, 2026"
_DAILY_CADENCE_TITLE_DATE_RE = re.compile(
    r'\(daily\)\s*-\s*([A-Za-z]+\s+\d{1,2},?\s*\d{4})', re.IGNORECASE,
)


def _detect_daily_cadence(url: str, body_text: str) -> bool:
    if not any(p in url.lower() for p in _DAILY_CADENCE_URL_ALLOWLIST):
        return False
    return bool(_DAILY_CADENCE_PHRASE_RE.search(body_text[:1000]))


def _url_is_daily_cadence_allowlisted(url: str) -> bool:
    return any(p in url.lower() for p in _DAILY_CADENCE_URL_ALLOWLIST)


def _fails_llm_recency_guard(val: str, url: str) -> bool:
    """True if an LLM-returned date should be rejected as a likely dynamic
    "as of today" pointer rather than a real refresh event — the same
    recency threshold _extract_from_html applies to regex-scanned body
    text, but that guard only covers Tiers 2/4's own extraction; the LLM
    tiers (3/4-fallback/5) had no equivalent check, so a page with a live
    "current as of" marker could make the model confidently return today's
    date as if it were a real answer."""
    days_old = (_date.today() - _date.fromisoformat(val)).days
    return days_old < _BODY_DATE_RECENCY_DAYS and not _url_is_daily_cadence_allowlisted(url)


def _parse_date(val: str | None) -> str | None:
    if not val:
        return None
    val = val.strip()
    if val.upper() in ("NONE", "N/A", "NA", ""):
        return None
    if re.fullmatch(r'\d{4}', val):
        return None
    try:
        # Without an explicit `default`, dateutil fills any date component
        # missing from the string (e.g. "July 2024" has no day) using
        # datetime.now() — silently manufacturing a false-precision date
        # that carries TODAY's day, not a real one. Anchoring to a distant
        # placeholder instead makes a missing-year string get rejected by
        # the year-range check below, and a missing-day string resolve to
        # day 1 of the month rather than an arbitrary "today" day.
        dt = _dateparser.parse(val, fuzzy=True, default=datetime(1900, 1, 1))
    except (_ParserError, OverflowError, ValueError):
        return None
    if not dt or not (2000 <= dt.year <= _date.today().year + 1):
        return None
    result = dt.strftime("%Y-%m-%d")
    if result > _TODAY:
        return None
    return result


def _first_year(text: str) -> str | None:
    m = _YEAR_RE.search(text)
    return m.group(1) if m else None


def _visible_text(soup: BeautifulSoup, max_chars: int = 500000) -> str:
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text(" ", strip=True)[:max_chars]


# Keywords in link text or href that suggest a sub-page with a data release date
_SUBLINK_TEXT_RE = re.compile(
    r'\b(?:data|dataset|download|release|latest|current|estimate|statistic|'
    r'table|report|publication|update|revised?|refresh|access|result|file)\b',
    re.IGNORECASE,
)
_SUBLINK_SKIP_RE = re.compile(
    r'\b(?:contact|about|faq|help|glossary|methodology|privacy|terms|'
    r'login|sign.?in|subscribe|newsletter|feedback|sitemap|careers|news)\b',
    re.IGNORECASE,
)

def _promising_sublinks(html: str, base_url: str, max_links: int = 6) -> list[str]:
    """Extract up to max_links internal links that are likely data-release sub-pages."""
    from urllib.parse import urljoin, urldefrag
    soup = BeautifulSoup(html, "html.parser")
    base_parsed = urlparse(base_url)
    seen: set[str] = set()
    scored: list[tuple[int, str]] = []

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "javascript:")):
            continue
        full = urldefrag(urljoin(base_url, href))[0]
        parsed = urlparse(full)
        # same domain only
        if parsed.netloc != base_parsed.netloc:
            continue
        # skip non-HTML extensions
        if parsed.path.lower().endswith((".pdf", ".zip", ".xlsx", ".csv", ".json", ".xml")):
            continue
        if full in seen or full == base_url:
            continue
        seen.add(full)

        link_text = (a.get_text(" ", strip=True) + " " + href).lower()
        if _SUBLINK_SKIP_RE.search(link_text):
            continue

        score = 0
        if _SUBLINK_TEXT_RE.search(link_text):
            score += 2
        # bonus if the href contains a year (likely a release page)
        if re.search(r'/20\d\d/', parsed.path):
            score += 1
        if score > 0:
            scored.append((score, full))

    scored.sort(key=lambda x: -x[0])
    return [url for _, url in scored[:max_links]]


def _extract_from_html(html: str, url: str) -> tuple[str | None, str, str | None]:
    """Return (date_string, source_label, cadence)."""
    soup = BeautifulSoup(html, "html.parser")

    # JSON-LD — reliable structured metadata
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
            for key in ("dateModified", "datePublished", "dateCreated"):
                val = _parse_date(data.get(key) or (data.get("@graph") or [{}])[0].get(key))
                if val:
                    return val, f"json-ld:{key}", None
        except Exception:
            pass

    # Site-specific metadata fields checked before generic Dublin Core.
    # cdc:last_updated is the actual data-content update date on CDC pages;
    # DC.date on CDC is the CMS file-publish timestamp (often wrong).
    for name in ("cdc:last_updated", "cdc:last_reviewed"):
        tag = soup.find("meta", attrs={"name": re.compile(name, re.I)})
        if tag:
            val = _parse_date(tag.get("content"))
            if val:
                return val, f"meta:{name}", None

    # Dublin Core / DCAT — reliable structured metadata
    for name in ("dcterms.modified", "dc.date", "dcterms.date", "DC.date"):
        tag = soup.find("meta", attrs={"name": re.compile(name, re.I)})
        if tag:
            val = _parse_date(tag.get("content"))
            if val:
                return val, f"meta:{name}", None

    # NOTE: article:modified_time / og:updated_time deliberately excluded —
    # these are CMS editorial timestamps that reflect when a page editor last
    # touched the HTML, NOT when the underlying dataset was refreshed.

    # Body-text: collect ALL matching dates, apply recency + page-meta filters,
    # return the most recent. Using max() fixes cases where old historical dates
    # (policy notes, methodology changes) appear before the current refresh date.
    body_text = _visible_text(soup, max_chars=20000)
    is_daily = _detect_daily_cadence(url, body_text)

    # Allowlisted daily feeds: the title line carries the exact release date
    # right next to the cadence marker — prefer it over the generic scan below.
    if is_daily:
        m = _DAILY_CADENCE_TITLE_DATE_RE.search(body_text[:1000])
        if m:
            val = _parse_date(m.group(1))
            if val:
                return val, "body-text:title-date (daily-cadence)", "daily"

    candidates: list[str] = []
    cutoff = (_date.today() - __import__("datetime").timedelta(days=_BODY_DATE_RECENCY_DAYS)).isoformat()
    pos = 0
    while True:
        m = _DATE_RE.search(body_text, pos)
        if not m:
            break
        window_before = body_text[max(0, m.start() - 25):m.start()]
        if _PAGE_META_RE.search(window_before):
            pos = m.end()
            continue
        parsed = _parse_date(m.group(1))
        # Recency guard skips "as of today" dynamic timestamps — except on
        # allowlisted daily feeds, where recency is the correct signal.
        if parsed and (is_daily or parsed <= cutoff):
            candidates.append(parsed)
        pos = m.end()

    if candidates:
        return max(candidates), "body-text", ("daily" if is_daily else None)

    return None, "", None


# ── tier implementations ──────────────────────────────────────────────────────

async def _tier1(session: aiohttp.ClientSession, url: str) -> dict | None:
    if urlparse(url).path.lower().endswith(tuple(_SKIP_LASTMOD_EXTS)):
        logger.debug(f"tier1 {url}: skipped (binary extension)")
        return None
    try:
        async with session.head(url, timeout=aiohttp.ClientTimeout(total=REQUEST_TO),
                                 allow_redirects=True, headers=HEADERS) as r:
            logger.debug(f"tier1 {url}: HEAD -> status={r.status}")
            lm = r.headers.get("Last-Modified") or r.headers.get("X-Last-Modified")
            if lm:
                val = _parse_date(lm)
                if val:
                    days_old = (_date.today() - _date.fromisoformat(val)).days
                    if days_old < 14:
                        logger.debug(f"tier1 {url}: Last-Modified={val} rejected (only {days_old}d old, likely dynamic regen)")
                        return None  # likely dynamic regeneration
                    logger.debug(f"tier1 {url}: Last-Modified={val} accepted")
                    return {"date": val, "source": "Last-Modified header", "tier": 1}
            logger.debug(f"tier1 {url}: no usable Last-Modified header")
    except Exception as e:
        logger.debug(f"tier1 {url}: EXCEPTION {type(e).__name__}: {e}")
    return None


async def _tier2(session: aiohttp.ClientSession, url: str) -> dict | None:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=REQUEST_TO),
                                allow_redirects=True, headers=HEADERS) as r:
            logger.debug(f"tier2 {url}: GET -> status={r.status} content_type={r.content_type}")
            if r.content_type and "html" not in r.content_type and "text" not in r.content_type:
                logger.debug(f"tier2 {url}: non-HTML response, skipping")
                return None
            html = await r.text(errors="replace")
            logger.debug(f"tier2 {url}: fetched {len(html)} bytes of HTML")

        # Try main page first
        date, src, cadence = _extract_from_html(html, url)
        if date:
            logger.debug(f"tier2 {url}: extracted date={date} via {src!r}")
            return {"date": date, "source": src, "tier": 2, "cadence": cadence, "_html": html}

        # Main page found nothing — follow promising sub-links in parallel
        sublinks = _promising_sublinks(html, url)
        logger.debug(f"tier2 {url}: main page had no date, following {len(sublinks)} sub-link(s): {sublinks}")
        if sublinks:
            async def _fetch_sub(sub_url: str) -> tuple[str | None, str, str | None]:
                try:
                    async with session.get(
                        sub_url,
                        timeout=aiohttp.ClientTimeout(total=REQUEST_TO),
                        allow_redirects=True, headers=HEADERS
                    ) as sr:
                        if sr.content_type and "html" not in sr.content_type and "text" not in sr.content_type:
                            return None, "", None
                        sub_html = await sr.text(errors="replace")
                    return _extract_from_html(sub_html, sub_url)
                except Exception as e:
                    logger.debug(f"tier2 sub-link {sub_url}: EXCEPTION {type(e).__name__}: {e}")
                    return None, "", None

            sub_results = await asyncio.gather(*[_fetch_sub(s) for s in sublinks])
            # Pick the most recent date across all sub-pages
            candidates = [(d, f"{s} (sub:{sub})", cad) for (d, s, cad), sub in zip(sub_results, sublinks) if d]
            if candidates:
                best_date, best_src, best_cadence = max(candidates, key=lambda x: x[0])
                logger.debug(f"tier2 {url}: best sub-link date={best_date} via {best_src!r}")
                return {"date": best_date, "source": best_src, "tier": 2, "cadence": best_cadence, "_html": html}

        logger.debug(f"tier2 {url}: no date found on main page or sub-links")
        return {"date": None, "source": "", "tier": 2, "cadence": None, "_html": html}  # pass html to tier3
    except Exception as e:
        logger.debug(f"tier2 {url}: EXCEPTION {type(e).__name__}: {e}")
    return None


def _tier3_sync(page_text: str, url: str) -> dict | None:
    """Groq openai/gpt-oss-120b NLP fallback — decoy-aware: a page often has
    several plausible-looking dates, and picking the wrong one is the
    documented failure mode (PIPELINE_CHANGELOG.md Problem 5), not a
    fetching problem. Model choice and prompt were validated empirically
    against real hard cases (see plan) before wiring in."""
    try:
        prompt = (
            f"URL: {url}\n"
            f"Page text (visible text only, up to 20000 chars):\n{page_text[:20000]}\n\n"
            "What is the LAST REFRESH / LAST UPDATED date for the DATASET or DATA on this page?\n\n"
            "This page likely contains several dates that are NOT the answer. Do NOT return:\n"
            "  - a copyright or footer year (e.g. \"© 2026\")\n"
            "  - a CMS editorial timestamp (\"page last reviewed\", \"page last revised\", \"last modified\" "
            "referring to the HTML/page itself, not the data)\n"
            "  - an unrelated news/article publish date\n"
            "  - a future \"next release\" / \"upcoming update\" date\n"
            "  - a historical footnote or per-row date elsewhere on the page that isn't the overall refresh date\n\n"
            "Return ONLY the date the underlying DATASET/DATA was actually last refreshed or updated. "
            "If no such date is genuinely present, return null rather than guessing.\n"
            "Return ONLY valid JSON, no markdown, no explanation: "
            '{"date": "YYYY-MM-DD or null", "source": "exact quoted phrase from the page text you used"}'
        )
        logger.info(f"tier3 {url}: invoking Groq openai/gpt-oss-120b (page_text_len={len(page_text)})")
        logger.debug(f"tier3 {url}: prompt sent:\n{prompt}")
        resp = _groq_client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
        raw = resp.choices[0].message.content.strip()
        logger.debug(f"tier3 {url}: raw response:\n{raw}")
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
        data = json.loads(raw)
        val = _parse_date(str(data.get("date") or ""))
        if val and _fails_llm_recency_guard(val, url):
            logger.info(f"tier3 {url}: rejecting date={val} — within {_BODY_DATE_RECENCY_DAYS}d of today "
                        f"and not on the daily-cadence allowlist, likely a dynamic 'as of today' pointer")
            val = None
        if val:
            logger.info(f"tier3 {url}: parsed date={val} source={data.get('source')!r}")
            return {"date": val, "source": data.get("source", "groq-text"), "tier": 3}
        logger.info(f"tier3 {url}: model returned no usable date (raw date field: {data.get('date')!r})")
    except Exception as e:
        logger.debug(f"tier3 {url}: EXCEPTION {type(e).__name__}: {e}")
    return None


async def _tier4(url: str) -> dict | None:
    try:
        from playwright.async_api import async_playwright
        logger.debug(f"tier4 {url}: launching headless Chromium")
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(2000)
            html = await page.content()
            await browser.close()
        logger.debug(f"tier4 {url}: rendered {len(html)} bytes of HTML")

        date, src, cadence = _extract_from_html(html, url)
        if date:
            logger.debug(f"tier4 {url}: extracted date={date} via {src!r} (no LLM needed)")
            return {"date": date, "source": src + " (playwright)", "tier": 4, "cadence": cadence}

        # Playwright page text → Groq text-reasoning fallback
        logger.debug(f"tier4 {url}: no date in rendered HTML, falling back to Groq text reasoning")
        soup = BeautifulSoup(html, "html.parser")
        text = _visible_text(soup)  # strips <script>/<style>/<noscript> before the LLM sees it
        async with _groq_text_sem:
            result = await asyncio.get_event_loop().run_in_executor(
                None, _tier3_sync, text, url)
        if result:
            result["tier"] = 4
            result["source"] += " (playwright+groq)"
            return result
        logger.debug(f"tier4 {url}: Groq fallback also found nothing")
    except Exception as e:
        logger.debug(f"tier4 {url}: EXCEPTION {type(e).__name__}: {e}")
    return None


def _tier5_sync(url: str) -> dict | None:
    """Groq compound-beta: real browser visit — handles bot walls and JS-heavy sites."""
    try:
        logger.info(f"tier5 {url}: invoking Groq compound-beta (real-browsing agent)")
        resp = _groq_client.chat.completions.create(
            model="compound-beta",
            messages=[{"role": "user", "content": (
                f"Visit this URL: {url}\n"
                "Find the LAST REFRESH / LAST UPDATED / DATA AS OF date for the DATASET on this page. "
                "Look in the header, footer, sidebar, table caption, or metadata. "
                "Do NOT return the page's HTML edit date, CMS timestamp, or a news article date. "
                "Return ONLY the date when the underlying dataset was last refreshed or updated.\n"
                "Return ONLY valid JSON with no markdown: "
                '{"date": "YYYY-MM-DD or null", "source": "exact text or element where you found it"}'
            )}],
        )
        raw = resp.choices[0].message.content.strip()
        logger.debug(f"tier5 {url}: raw response:\n{raw}")
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
        data = json.loads(raw)
        val = _parse_date(str(data.get("date") or ""))
        if val and _fails_llm_recency_guard(val, url):
            logger.info(f"tier5 {url}: rejecting date={val} — within {_BODY_DATE_RECENCY_DAYS}d of today "
                        f"and not on the daily-cadence allowlist, likely a dynamic 'as of today' pointer")
            val = None
        if val:
            logger.info(f"tier5 {url}: parsed date={val} source={data.get('source')!r}")
            return {"date": val, "source": data.get("source", "groq"), "tier": 5}
        logger.info(f"tier5 {url}: model returned no usable date (raw date field: {data.get('date')!r})")
    except Exception as e:
        logger.debug(f"tier5 {url}: EXCEPTION {type(e).__name__}: {e}")
    return None


# ── per-URL orchestrator ──────────────────────────────────────────────────────

_domain_sems: dict[str, asyncio.Semaphore] = defaultdict(lambda: asyncio.Semaphore(DOMAIN_LIMIT))
_groq_sem: asyncio.Semaphore | None = None        # initialised in main() — Tier 5 compound-beta
_groq_text_sem: asyncio.Semaphore | None = None   # initialised in main() — Tier 3/4 gpt-oss-120b

async def process_url(session: aiohttp.ClientSession, url: str,
                       global_sem: asyncio.Semaphore, tier_max: int) -> dict:
    if classify_url(url) == "catalog":
        logger.info(f"process_url {url}: skipped — classified as catalog/browse page")
        return {
            "url": url, "date": None, "source": None, "tier": None,
            "error": "catalog/browse URL — no single dataset date exists, needs a corrected provenance URL",
            "tiers_attempted": "", "extraction_time_sec": 0.0,
        }

    if classify_blocker(url) == "not_trackable":
        logger.info(f"process_url {url}: skipped — not externally trackable")
        return {
            "url": url, "date": None, "source": None, "tier": None,
            "error": "no external source — not programmatically trackable",
            "tiers_attempted": "", "extraction_time_sec": 0.0,
        }

    domain = urlparse(url).netloc
    async with global_sem, _domain_sems[domain]:
        t0 = _time.time()
        tried = []
        result = {"url": url, "date": None, "source": None, "tier": None, "error": None, "cadence": None}

        def _done():
            result["tiers_attempted"]     = ",".join(str(x) for x in tried)
            result["extraction_time_sec"] = round(_time.time() - t0, 2)
            if not result["date"] and tried:
                result["error"] = f"no date after tiers {result['tiers_attempted']}"
            if result["date"]:
                logger.info(f"process_url {url}: RESOLVED tier={result['tier']} date={result['date']} "
                            f"source={result['source']!r} tiers_attempted={result['tiers_attempted']} "
                            f"time={result['extraction_time_sec']}s")
            else:
                logger.info(f"process_url {url}: NO DATE FOUND tiers_attempted={result['tiers_attempted']} "
                            f"time={result['extraction_time_sec']}s")
            return result

        # Tier 0: domain-specific handlers (P0/P1 fixes, see specialized_source_handlers.py).
        # No LLM, at most one HTTP call — matched-but-empty falls through to tiers 1-5,
        # same as if this dispatch didn't exist.
        for pattern, handler in SPECIALIZED_HANDLERS:
            if not pattern.search(url):
                continue
            logger.debug(f"tier0 {url}: matched handler {handler.__name__}")
            tried.append(0)
            r0 = await handler(url, session)
            if r0 and r0.get("_redirect_url"):
                logger.debug(f"tier0 {url}: {handler.__name__} redirected to {r0['_redirect_url']}")
                r1_redirect = await _tier1(session, r0["_redirect_url"])
                if r1_redirect:
                    r1_redirect["source"] = f"{r1_redirect.get('source', 'Last-Modified header')} (via {r0['_redirect_url']})"
                    result.update(r1_redirect)
                    return _done()
            elif r0 and r0.get("date"):
                logger.debug(f"tier0 {url}: {handler.__name__} found date={r0.get('date')}")
                result.update(r0)
                return _done()
            else:
                logger.debug(f"tier0 {url}: {handler.__name__} matched but found nothing, falling through to tiers 1-5")
            break

        # Run T1 + T2 together. Priority:
        #   1. T2 structured/body-text date (most reliable)
        #   2. T1 Last-Modified ONLY if T2 completely failed (exception or non-HTML)
        #      — never use Last-Modified as fallback when T2 fetched HTML successfully,
        #        because Last-Modified reflects page regeneration, not data release.
        tried += [1, 2]
        r1, r2 = await asyncio.gather(
            _tier1(session, url),
            _tier2(session, url),
        )
        html_cache = None
        t2_got_html = False
        if r2:
            html_cache = r2.pop("_html", None)
            t2_got_html = html_cache is not None
            if r2["date"]:
                result.update(r2); return _done()
        if r1 and not t2_got_html:
            # T2 errored or got non-HTML — Last-Modified is the only signal
            result.update(r1); return _done()
        if tier_max < 3: return _done()

        if html_cache:
            tried.append(3)
            soup = BeautifulSoup(html_cache, "html.parser")
            text = _visible_text(soup)  # strips <script>/<style>/<noscript> before the LLM sees it
            async with _groq_text_sem:
                r3 = await asyncio.get_event_loop().run_in_executor(
                    None, _tier3_sync, text, url)
            if r3: result.update(r3); return _done()
        if tier_max < 4: return _done()

        tried.append(4)
        r4 = await _tier4(url)
        if r4: result.update(r4); return _done()
        if tier_max < 5: return _done()

        tried.append(5)
        async with _groq_sem:
            r5 = await asyncio.get_event_loop().run_in_executor(None, _tier5_sync, url)
        if r5: result.update(r5); return _done()

        # Absolute last resort: Census ACS vintage year embedded in the URL's
        # tid= param. It's a coarse year-only guess, not a real refresh
        # signal, so it must never preempt a more precise date any other
        # tier found — only reached when tiers 1-5 all found nothing.
        r0_census = await handle_census_url_vintage(url, session)
        if r0_census and r0_census.get("date"):
            logger.debug(f"tier0-last-resort {url}: Census vintage-year fallback found {r0_census.get('date')}")
            tried.append(0)
            result.update(r0_census)
        return _done()


# ── main ─────────────────────────────────────────────────────────────────────

def load_urls(csv_path: str) -> dict[str, list[str]]:
    """Returns {url: [dataset_id, ...]} deduplicated.

    Comma-separated multi-URL provenance fields are split into separate
    entries (see specialized_source_handlers.normalize_url) rather than
    treated as one unparseable URL string.
    """
    url_map: dict[str, list[str]] = defaultdict(list)
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            raw_url = row.get("provenance_url", "").strip().strip('"')
            did = row.get("id", "").strip()
            if not raw_url:
                continue
            for url in normalize_url(raw_url):
                if url.startswith("http"):
                    url_map[url].append(did)
    return url_map


async def main(tier_max: int, resume: bool, input_csv: str = None, output_json: str = None):
    log_path = setup_logging()
    print(f"Detailed run log -> {log_path}")
    logger.info(f"=== run started === csv={input_csv or INPUT_CSV} output={output_json or OUTPUT_JSON} tier_max={tier_max} resume={resume}")

    global _groq_sem, _groq_text_sem
    _groq_sem = asyncio.Semaphore(GROQ_CONCURRENCY)
    _groq_text_sem = asyncio.Semaphore(GROQ_TEXT_CONCURRENCY)
    csv_path = input_csv or INPUT_CSV
    out_path = output_json or OUTPUT_JSON
    url_map = load_urls(csv_path)
    all_urls = list(url_map.keys())
    print(f"Loaded {len(all_urls)} unique URLs ({sum(len(v) for v in url_map.values())} total rows)")

    catalog_urls = [u for u in all_urls if classify_url(u) == "catalog"]
    if catalog_urls:
        print(f"\n{len(catalog_urls)} URL(s) classified as catalog/browse — skipped, no single dataset date:")
        for u in catalog_urls:
            print(f"  {u}  (ids: {', '.join(url_map[u])})")
        report_path = os.path.splitext(out_path)[0] + "_catalog_report.json"
        with open(report_path, "w") as f:
            json.dump(
                [{"url": u, "dataset_ids": url_map[u]} for u in catalog_urls],
                f, indent=2,
            )
        print(f"Catalog URL report saved -> {report_path}\n")

    # Load existing results if resuming
    existing: dict = {}
    if resume and os.path.exists(out_path):
        with open(out_path) as f:
            existing = json.load(f)
        done_urls = {v["url"] for v in existing.values() if v.get("last_refresh_date")}
        all_urls = [u for u in all_urls if u not in done_urls]
        print(f"Resuming — {len(all_urls)} URLs remaining")

    global_sem = asyncio.Semaphore(CONCURRENCY)
    connector = aiohttp.TCPConnector(ssl=False, limit=CONCURRENCY)

    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [process_url(session, url, global_sem, tier_max) for url in all_urls]
        results_list = []
        done = 0
        for coro in asyncio.as_completed(tasks):
            r = await coro
            results_list.append(r)
            done += 1
            tier_label = f"T{r['tier']}" if r["tier"] is not None else "miss"
            print(f"  [{done}/{len(all_urls)}] {tier_label}  {r['date'] or '—':<12}  {r['url'][:70]}")

    # Build output keyed by dataset_id
    output = dict(existing)
    url_results = {r["url"]: r for r in results_list}
    for url, dataset_ids in url_map.items():
        r = url_results.get(url)
        if r is None:
            continue  # was already in existing
        for did in dataset_ids:
            output[did] = {
                "url":                 url,
                "last_refresh_date":   r["date"],
                "date_source":         r["source"],
                "tier_used":           r["tier"],
                "date_found":          bool(r["date"]),
                "tiers_attempted":     r.get("tiers_attempted", ""),
                "tier_failed_reason":  r.get("error"),
                "extraction_time_sec": r.get("extraction_time_sec"),
            }

    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)

    # Summary
    found    = sum(1 for v in output.values() if v.get("last_refresh_date"))
    by_tier  = defaultdict(int)
    for v in output.values():
        by_tier[v.get("tier_used")] += 1

    print(f"\n{'─'*50}")
    print(f"Results saved → {out_path}")
    print(f"Found date : {found}/{len(output)} datasets ({found*100//max(len(output),1)}%)")
    for t in sorted(k for k in by_tier if k is not None):
        print(f"  Tier {t}    : {by_tier[t]}")
    print(f"  No date  : {by_tier[None]}")

    logger.info(f"=== run finished === found={found}/{len(output)} by_tier={dict(by_tier)} output={out_path}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv",       default=INPUT_CSV,   help="Provenance CSV to read")
    ap.add_argument("--output",    default=OUTPUT_JSON, help="Output JSON path")
    ap.add_argument("--tier-max",  type=int, default=TIER_MAX)
    ap.add_argument("--resume",    action="store_true")
    args = ap.parse_args()
    asyncio.run(main(tier_max=args.tier_max, resume=args.resume,
                     input_csv=args.csv, output_json=args.output))
