"""
Domain-specific date handlers — P0/P1 fixes from REFRESH_DATE_BLOCKERS.md.

These bypass the generic 5-tier cascade in provenance_refresh_extractor.py for
URL patterns where the tiered approach structurally cannot work (wrong URL
target, JS SPA shell, dead API) but a known, deterministic fix exists: a
specific API endpoint, a value embedded in the URL itself, or a URL
correction. No LLM calls, no crawling — one HTTP round trip (or none) per URL.

Each handler returns the same result shape the tier functions use
(date/source/tier/cadence) so it drops into process_url()'s existing
result.update()/_done() bookkeeping unchanged. Returning None means "this
handler's own lookup failed" — the caller falls through to the normal
tier 1-5 cascade rather than giving up.
"""

import os
import re
import json
from datetime import date as _date

import aiohttp
from bs4 import BeautifulSoup
from dateutil import parser as _dateparser
from dateutil.parser import ParserError as _ParserError

REQUEST_TO = 10
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; StalenesBot/1.0)"}
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")


def _parse_date(val: str | None) -> str | None:
    """Same fuzzy-parse + sanity guards as provenance_refresh_extractor._parse_date,
    duplicated here to keep this module import-independent."""
    if not val:
        return None
    val = str(val).strip()
    if val.upper() in ("NONE", "N/A", "NA", ""):
        return None
    try:
        parsed = _dateparser.parse(val, fuzzy=True)
    except (_ParserError, ValueError, OverflowError):
        return None
    if not parsed:
        return None
    if parsed.year < 2000 or parsed.year > _date.today().year + 1:
        return None
    iso = parsed.date().isoformat()
    if iso > _date.today().isoformat():
        return None
    return iso


# ── URL normalization (runs at load time, before any dispatch/tiers) ──────────

def normalize_url(url: str) -> list[str]:
    """Returns one or more corrected URLs for a single raw provenance_url value.

    - Splits comma-separated multi-URL provenance fields into separate URLs
      (the current pipeline treats the whole string as one URL and fails).
    - Rewrites dead http:// FBI URLs to https:// (the https path is known to
      work; the http path times out / dead-ends per REFRESH_DATE_BLOCKERS.md #17).
    """
    url = url.strip()
    if "," in url:
        return [u.strip() for u in url.split(",") if u.strip()]
    if url.startswith("http://www.fbi.gov") or url.startswith("http://fbi.gov"):
        return [url.replace("http://", "https://", 1)]
    return [url]


# ── structural "no date exists" classification ────────────────────────────────

_NOT_TRACKABLE_RE = re.compile(r"^https?://(www\.)?datacommons\.org/?$", re.IGNORECASE)


def classify_blocker(url: str) -> str | None:
    """Returns 'not_trackable' for URLs with no external refresh signal at all,
    else None. Mirrors classify_url()'s existing catalog-skip pattern in
    provenance_refresh_extractor.py — same style, checked at the same call site."""
    if _NOT_TRACKABLE_RE.match(url.strip()):
        return "not_trackable"
    return None


# ── handler: Census vintage year embedded in URL (no HTTP needed) ─────────────

_CENSUS_TID_RE = re.compile(r"[A-Z]{2,6}5Y(\d{4})")


async def handle_census_url_vintage(url: str, session: aiohttp.ClientSession | None = None) -> dict | None:
    m = _CENSUS_TID_RE.search(url)
    if not m:
        return None
    year = m.group(1)
    if not (2000 <= int(year) <= _date.today().year + 1):
        return None
    return {"date": year, "source": "url:tid-vintage-year", "tier": 0, "cadence": None}


# ── handler: Wikidata homepage -> query.wikidata.org redirect ─────────────────

_WIKIDATA_HOMEPAGE_RE = re.compile(r"^https?://(www\.)?wikidata\.org/?(wiki/Wikidata:Main_Page)?/?$", re.IGNORECASE)


async def handle_wikidata(url: str, session: aiohttp.ClientSession | None = None) -> dict | None:
    """Not a date lookup itself — signals process_url to re-run Tier 1's HEAD
    request against query.wikidata.org instead of the (dateless) homepage."""
    if not _WIKIDATA_HOMEPAGE_RE.match(url.strip()):
        return None
    return {"_redirect_url": "https://query.wikidata.org/"}


# ── handler: NASA NCCS NEX products via Earthdata CMR API ─────────────────────

async def handle_nasa_nccs(url: str, session: aiohttp.ClientSession) -> dict | None:
    if "nccs.nasa.gov" not in url:
        return None
    low = url.lower()
    if "nex-gddp" in low:
        short_name = "NEX-GDDP"
    elif "nex-dcp30" in low:
        short_name = "NEX-DCP30"
    else:
        return None

    # .json's "time_end" is the dataset's temporal coverage end (e.g. a 2100
    # climate-projection horizon), not a refresh signal — use .umm_json's
    # meta.revision-date, the actual metadata-record last-updated timestamp.
    api_url = f"https://cmr.earthdata.nasa.gov/search/collections.umm_json?short_name={short_name}"
    try:
        async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=REQUEST_TO),
                                headers=HEADERS) as r:
            if r.status != 200:
                return None
            data = await r.json(content_type=None)
    except Exception:
        return None

    items = data.get("items") or []
    if not items:
        return None
    meta = items[0].get("meta") or {}
    val = _parse_date(meta.get("revision-date"))
    if val:
        return {"date": val, "source": "nasa-cmr:revision-date", "tier": 0, "cadence": None}
    return None


# ── handler: HUMDATA (HDX) dataset pages via CKAN API ──────────────────────────

_HUMDATA_SLUG_RE = re.compile(r"data\.humdata\.org/dataset/([\w-]+)")


async def handle_humdata(url: str, session: aiohttp.ClientSession) -> dict | None:
    m = _HUMDATA_SLUG_RE.search(url)
    if not m:
        return None
    slug = m.group(1)
    api_url = f"https://data.humdata.org/api/3/action/package_show?id={slug}"
    try:
        async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=REQUEST_TO),
                                headers=HEADERS) as r:
            if r.status != 200:
                return None
            data = await r.json(content_type=None)
    except Exception:
        return None

    if not data.get("success"):
        return None
    result = data.get("result") or {}
    val = _parse_date(result.get("metadata_modified"))
    if val:
        return {"date": val, "source": "humdata-ckan:metadata_modified", "tier": 0, "cadence": None}
    return None


# ── handler: NDAP India dataset pages via snippet API ──────────────────────────

_NDAP_ID_RE = re.compile(r"ndap\.niti\.gov\.in/dataset/(\d+)")


async def handle_ndap(url: str, session: aiohttp.ClientSession) -> dict | None:
    m = _NDAP_ID_RE.search(url)
    if not m:
        return None
    dataset_id = m.group(1)
    api_url = f"https://ndap.niti.gov.in/api/1/util/snippet/?id={dataset_id}"
    try:
        async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=REQUEST_TO),
                                headers=HEADERS) as r:
            if r.status != 200:
                return None
            data = await r.json(content_type=None)
    except Exception:
        return None

    # Response shape is undocumented — search common key names defensively,
    # including one level of nesting (e.g. {"data": {...}}).
    candidates = [data] + [v for v in data.values() if isinstance(v, dict)]
    for c in candidates:
        for key in ("last_updated", "lastUpdated", "updated_at", "updatedAt"):
            val = _parse_date(c.get(key))
            if val:
                return {"date": val, "source": f"ndap-api:{key}", "tier": 0, "cadence": None}
    return None


# ── handler: GitHub tree pages via Commits API ─────────────────────────────────

_GITHUB_TREE_RE = re.compile(
    r"github\.com/([\w.-]+)/([\w.-]+)/tree/[\w.-]+/(.+?)/?$"
)


async def handle_github_tree(url: str, session: aiohttp.ClientSession) -> dict | None:
    m = _GITHUB_TREE_RE.search(url)
    if not m:
        return None
    owner, repo, path = m.groups()
    from urllib.parse import unquote
    path = unquote(path)
    api_url = f"https://api.github.com/repos/{owner}/{repo}/commits"
    headers = dict(HEADERS)
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"
    try:
        async with session.get(api_url, params={"path": path, "per_page": 1},
                                timeout=aiohttp.ClientTimeout(total=REQUEST_TO),
                                headers=headers) as r:
            if r.status != 200:
                return None
            data = await r.json(content_type=None)
    except Exception:
        return None

    if not data:
        return None
    commit = (data[0] or {}).get("commit") or {}
    for who in ("committer", "author"):
        val = _parse_date((commit.get(who) or {}).get("date"))
        if val:
            return {"date": val, "source": f"github-commits-api:{who}", "tier": 0, "cadence": None}
    return None


# ── handler: EPA FTP-over-HTTPS directory listing ──────────────────────────────

_FTP_TIMESTAMP_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\s+\d{2}:\d{2}")


async def handle_epa_ftp(url: str, session: aiohttp.ClientSession) -> dict | None:
    if "gaftp.epa.gov" not in url:
        return None
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=REQUEST_TO),
                                headers=HEADERS) as r:
            if r.status != 200:
                return None
            html = await r.text(errors="replace")
    except Exception:
        return None

    timestamps = _FTP_TIMESTAMP_RE.findall(html)
    if not timestamps:
        return None
    best = max(t for t in timestamps if _parse_date(t))
    val = _parse_date(best)
    if val:
        return {"date": val, "source": "epa-ftp:directory-listing", "tier": 0, "cadence": None}
    return None


# ── dispatch table ──────────────────────────────────────────────────────────────

# NOTE: handle_census_url_vintage is deliberately NOT in this eager dispatch
# list. It's a pure URL-regex guess (the ACS 5-Year vintage label, not an
# actual refresh timestamp) and is coarser than what Tiers 1-2 sometimes
# already find for these same URLs (e.g. a precise CMS-published date). It's
# wired in as a zero-cost fallback *after* Tiers 1-2 fail, not as a
# short-circuit ahead of them — see process_url(). The other 6 handlers hit
# an authoritative source-specific API and are safe to short-circuit eagerly.
SPECIALIZED_HANDLERS: list[tuple[re.Pattern, "callable"]] = [
    (_WIKIDATA_HOMEPAGE_RE,                                handle_wikidata),
    (re.compile(r"nccs\.nasa\.gov"),                       handle_nasa_nccs),
    (_HUMDATA_SLUG_RE,                                     handle_humdata),
    (_NDAP_ID_RE,                                          handle_ndap),
    (_GITHUB_TREE_RE,                                      handle_github_tree),
    (re.compile(r"gaftp\.epa\.gov"),                       handle_epa_ftp),
]
