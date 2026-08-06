"""Find the last observation date in a downloaded data file — plain Gemini
API calls, no coding agent, no subprocess. Replaces the old pi-coding-agent
subprocess approach (pi_date_extractor.py, deleted) after that tool was
found to violate policy for use on Google source/data, regardless of where
it's hosted.

Two-step design, not one shot — an LLM asked to eyeball a "max date" from a
truncated file preview can't see rows outside that preview, so it can't
reliably find a true max in an unsorted/grouped file. Splitting the job
avoids that:

  Step A (LLM): shown only column names, dtypes, and a small sample — picks
      WHICH column(s) represent the observation period (a semantic judgment
      call it's well-suited for; row volume doesn't help this decision).
  Step B (LLM again, different question): reads the picked column(s) across
      the WHOLE file (not the preview) and extracts the full, deduplicated
      set of distinct values that actually occur — then asks Gemini 3.1 Pro
      to identify the maximum from that complete list. An earlier version
      of Step B tried to compute the max in plain pandas/dateutil instead;
      that broke on a real 80k-row UN data export whose "Year" column
      silently mixed types (some rows numeric, some not) — one of
      presumably many such real-world messiness cases a strong model
      handles far more robustly than bespoke parsing code would, especially
      since it's still seeing every distinct value in the file, not a
      truncated sample, so the original "can't see a buried max" bug this
      design exists to fix stays fixed either way.

Setup:
  export GEMINI_API_KEY=...   (or set it in .env)
"""

import json
import os
import re
from datetime import datetime

import pandas as pd
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

LOGS_DIR = os.path.join(os.path.dirname(__file__), "logs")
MODEL = "gemini-3.1-pro-preview"
PEEK_ROWS = 200   # bounded, cheap read regardless of file size — used only
                  # to build Step A's prompt, never to compute the max
MAX_UNIQUE_VALUES = 15000   # safety cap on Step B's value list for a genuinely
                            # pathological column (e.g. near-per-row-unique
                            # timestamps) — real observation-period columns
                            # are almost always far coarser-grained than this

_api_key = os.environ.get("GEMINI_API_KEY", "")
if not _api_key:
    raise SystemExit("GEMINI_API_KEY is not set (checked .env and environment)")
_client = genai.Client(api_key=_api_key)

_CONFIG = types.GenerateContentConfig(
    thinking_config=types.ThinkingConfig(include_thoughts=True),
    temperature=0,
)

_STRATEGY_PROMPT = """A dataset file was downloaded. Below is a PREVIEW only — the
first {n} rows, not the whole file.

Columns: {columns}
Dtypes:
{dtypes}

First 5 rows of the preview:
{head}

Last 5 rows of the preview (NOT necessarily the true end of the file):
{tail}

Task: identify which column(s) represent the OBSERVATION PERIOD — the
year/date/period the DATA describes, not when it was published or sourced.

## Column priority rules (follow strictly)
1. PREFER columns named: Year, Date, Period, Observation_Date, Ref_Date, Reference_Period, Time_Period
2. IGNORE columns named: Source_Year, Publication_Year, Reported_Year, Data_Year, Access_Date
3. If both "Year" and "Source Year" exist -> use ONLY "Year"

## Shape patterns — pick exactly one
- "wide": years are the column HEADERS themselves (e.g. columns literally named
  "2020", "2021", "2022") -> list every year-like column header you see, even ones
  that look empty in this preview (a real value may appear elsewhere in the file).
- "single": one column holds a repeated date/year value -> list that one column name.
- "split": year and month are in two separate columns -> list [year_column, month_column].
- "not_possible": none of the above genuinely applies.
{exclude_note}
Do NOT try to compute the max value yourself — you are only picking column(s);
the actual max will be computed afterward by reading the full file in code.

Return ONLY this JSON, no markdown:
{{"strategy": "wide|single|split|not_possible", "columns": ["..."], "reasoning": "..."}}
"""


def _noop_log(_msg: str) -> None:
    pass


def _read_ext(filepath: str) -> str:
    return os.path.splitext(filepath)[1].lower()


def _read_peek(filepath: str, log_fn=_noop_log) -> "pd.DataFrame | None":
    ext = _read_ext(filepath)
    try:
        if ext == ".tsv":
            return pd.read_csv(filepath, sep="\t", nrows=PEEK_ROWS)
        if ext in (".xlsx", ".xls"):
            df = pd.read_excel(filepath)
            return df.head(PEEK_ROWS)
        if ext == ".json":
            return pd.read_json(filepath).head(PEEK_ROWS)
        if ext == ".parquet":
            return pd.read_parquet(filepath).head(PEEK_ROWS)
        # .csv or unknown extension — try csv as the default guess
        return pd.read_csv(filepath, nrows=PEEK_ROWS)
    except Exception as e:
        log_fn(f"Could not read a preview of {filepath}: {type(e).__name__}: {e}")
        return None


def _read_columns(filepath: str, cols: list[str]) -> "pd.DataFrame | None":
    """Reads ONLY the given columns across the WHOLE file (not a preview) —
    cheap even on a large file since unused columns are never loaded, and
    exact since no row-count cap is applied here. Read as strings uniformly,
    not pandas' auto-inferred dtypes — confirmed live on a real 80k-row UN
    data export that a single column can silently mix types (some rows
    numeric, some not), which crashed a plain numeric .max() with a
    TypeError; string dtype avoids that read-time crash regardless of how
    the values are later interpreted."""
    ext = _read_ext(filepath)
    try:
        if ext == ".tsv":
            return pd.read_csv(filepath, sep="\t", usecols=cols, dtype=str)
        if ext in (".xlsx", ".xls"):
            return pd.read_excel(filepath, usecols=cols, dtype=str)
        if ext == ".json":
            return pd.read_json(filepath, dtype=str)[cols]
        if ext == ".parquet":
            return pd.read_parquet(filepath, columns=cols).astype(str)
        return pd.read_csv(filepath, usecols=cols, dtype=str)
    except Exception:
        return None


def _year_key(header: str) -> tuple:
    """Sort key for a wide-format year-like column header (e.g. "2020",
    "FY2020", "2020Q1") — pulls the first 4-digit year out, falls back to
    the raw string so non-matching headers still sort (just last). Column
    HEADERS (not data values) are simple, clean strings by construction, so
    this stays a plain regex — the messiness that pushed Step B onto the LLM
    lives in actual data cells, not header names."""
    m = re.search(r"(19|20)\d{2}", str(header))
    return (1, int(m.group())) if m else (0, str(header))


_MAX_VALUE_PROMPT = """The column "{column}" was identified as representing a
dataset's observation period. Below are ALL {n} distinct values that occur in
this column across the ENTIRE file (deduplicated, not a sample):

{values}

Task: identify the MOST RECENT (maximum / latest) value in this list. Handle
whatever messiness is present yourself — mixed formats, stray whitespace,
inconsistent precision (some plain years, some full dates), etc. — and
return the value EXACTLY as it appears above (don't reformat it).

Return ONLY this JSON, no markdown:
{{"max_value": "<the exact value from the list above that is most recent>"}}
"""


def _ask_max_value(column_desc: str, values: list[str], log_fn=_noop_log) -> "str | None":
    """Step B — instead of hand-parsing every possible real-world date/year
    format ourselves (mixed types, locale differences, stray formatting —
    a genuinely open-ended list of edge cases), hand Gemini 3.1 Pro the full,
    deduplicated set of values that actually occur in the identified column
    across the WHOLE file (never a truncated sample — that's what keeps the
    original "can't see a buried max" bug fixed) and let the model determine
    which one is most recent. Rejects anything the model returns that isn't
    literally one of the input values, as a guard against hallucination."""
    prompt = _MAX_VALUE_PROMPT.format(column=column_desc, n=len(values), values="\n".join(values))
    log_fn(f"=== STEP B: max-value prompt for column {column_desc!r} ({len(values)} distinct values) ===")
    response = _client.models.generate_content(model=MODEL, contents=prompt, config=_CONFIG)
    parts = response.candidates[0].content.parts
    thought = "".join(p.text or "" for p in parts if getattr(p, "thought", False))
    final = "".join(p.text or "" for p in parts if not getattr(p, "thought", False))
    if thought:
        log_fn(f"THOUGHT: {thought}")
    log_fn(f"Raw response: {final}")
    cleaned = re.sub(r"^```(?:json)?|```$", "", final.strip(), flags=re.MULTILINE).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        log_fn("Could not parse a max_value from the response")
        return None
    val = str(data.get("max_value") or "").strip()
    if val not in values:
        log_fn(f"Model returned {val!r}, which isn't one of the input values — rejecting (hallucination guard)")
        return None
    return val


def _ask_strategy(peek: "pd.DataFrame", log_fn=_noop_log, exclude: list[str] | None = None) -> dict:
    exclude_note = (
        f"\nNOTE: columns {exclude} were already tried and were WRONG — pick a different one.\n"
        if exclude else ""
    )
    prompt = _STRATEGY_PROMPT.format(
        n=len(peek), columns=list(peek.columns), dtypes=peek.dtypes.to_string(),
        head=peek.head(5).to_string(), tail=peek.tail(5).to_string(),
        exclude_note=exclude_note,
    )
    log_fn(f"=== STEP A: strategy prompt ===\n{prompt}")
    response = _client.models.generate_content(model=MODEL, contents=prompt, config=_CONFIG)
    parts = response.candidates[0].content.parts
    thought = "".join(p.text or "" for p in parts if getattr(p, "thought", False))
    final = "".join(p.text or "" for p in parts if not getattr(p, "thought", False))
    if thought:
        log_fn(f"THOUGHT: {thought}")
    log_fn(f"Raw response: {final}")
    cleaned = re.sub(r"^```(?:json)?|```$", "", final.strip(), flags=re.MULTILINE).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return {"strategy": "not_possible", "columns": [], "reasoning": "unparseable response"}
    return data


def _cap_unique_values(uniques: list[str], log_fn=_noop_log, label: str = "") -> list[str]:
    if len(uniques) <= MAX_UNIQUE_VALUES:
        return uniques
    log_fn(f"{label} has {len(uniques)} distinct values — capping to the "
           f"{MAX_UNIQUE_VALUES} lexicographically-largest before asking Gemini "
           f"(a genuine edge case; most observation-period columns have far fewer)")
    return sorted(uniques)[-MAX_UNIQUE_VALUES:]


def _compute_max(filepath: str, strategy: dict, log_fn=_noop_log) -> tuple[str | None, str]:
    """Step B. Returns (date_str_or_None, column_used). "wide" stays a plain
    Python comparison (column HEADERS, not data values — simple/clean by
    construction); "single"/"split" hand the full, deduplicated set of
    real data values to Gemini (see _ask_max_value) rather than parsing
    them with bespoke code."""
    kind = strategy.get("strategy")
    cols = strategy.get("columns") or []

    if kind == "wide" and cols:
        df = _read_columns(filepath, cols)
        if df is None:
            log_fn(f"Could not read wide columns {cols}")
            return None, ",".join(cols)
        populated = [c for c in cols if c in df.columns and df[c].notna().any()]
        if not populated:
            log_fn(f"None of the candidate wide columns {cols} have any real data")
            return None, ",".join(cols)
        best = max(populated, key=_year_key)
        log_fn(f"Wide format — populated candidates {populated}, last one -> {best}")
        return str(best), best

    if kind == "single" and cols:
        col = cols[0]
        df = _read_columns(filepath, [col])
        if df is None or col not in df.columns:
            log_fn(f"Could not read single column {col!r}")
            return None, col
        uniques = sorted({v.strip() for v in df[col].dropna().astype(str) if v.strip()})
        if not uniques:
            log_fn(f"Column {col!r} has no non-null values")
            return None, col
        uniques = _cap_unique_values(uniques, log_fn, label=f"Column {col!r}")
        best = _ask_max_value(col, uniques, log_fn)
        if best is None:
            log_fn(f"Gemini could not identify a max value for column {col!r}")
            return None, col
        log_fn(f"Single column {col!r} -> {best} (from {len(uniques)} distinct full-file values)")
        return best, col

    if kind == "split" and len(cols) >= 2:
        year_col, month_col = cols[0], cols[1]
        df = _read_columns(filepath, [year_col, month_col])
        if df is None or year_col not in df.columns:
            log_fn(f"Could not read split columns {year_col!r}/{month_col!r}")
            return None, f"{year_col}+{month_col}"
        combos = set()
        for _, row in df.iterrows():
            y = str(row.get(year_col, "")).strip()
            if not y or y.lower() in ("nan", "none"):
                continue
            m = str(row.get(month_col, "")).strip() if month_col in df.columns else ""
            combos.add(f"{y}-{m}" if m and m.lower() not in ("nan", "none") else y)
        if not combos:
            log_fn(f"No usable values in {year_col!r}/{month_col!r}")
            return None, f"{year_col}+{month_col}"
        uniques = _cap_unique_values(sorted(combos), log_fn, label=f"Columns {year_col!r}+{month_col!r}")
        best = _ask_max_value(f"{year_col}+{month_col}", uniques, log_fn)
        if best is None:
            log_fn(f"Gemini could not identify a max value for {year_col!r}+{month_col!r}")
            return None, f"{year_col}+{month_col}"
        log_fn(f"Split year+month -> {best} (from {len(uniques)} distinct full-file combinations)")
        return best, f"{year_col}+{month_col}"

    log_fn(f"Strategy {kind!r} — nothing to compute")
    return None, "none"


def _extract_one_file(filepath: str, ground_truth: "str | None", max_retries: int, log_fn) -> tuple[str, str]:
    peek = _read_peek(filepath, log_fn)
    if peek is None or peek.empty:
        log_fn(f"No usable preview for {filepath} — skipping")
        return "not_possible", "none"

    tried_cols: list[str] = []
    date, col = "not_possible", "none"
    for attempt in range(1, max_retries + 1):
        log_fn(f"--- attempt {attempt}/{max_retries} ---")
        strategy = _ask_strategy(peek, log_fn, exclude=tried_cols or None)
        log_fn(f"Strategy chosen: {strategy}")
        tried_cols.extend(strategy.get("columns") or [])
        result, col = _compute_max(filepath, strategy, log_fn)
        date = result or "not_possible"
        log_fn(f"attempt {attempt}: date={date} column={col}")
        if ground_truth is None or str(date) == str(ground_truth):
            break
        log_fn(f"WRONG — expected {ground_truth}, retrying with a different column...")
    return date, col


def extract_date_from_file(
    filepath: "str | list[str]",
    ground_truth: "str | None" = None,
    max_retries: int = 3,
    timeout: int = 300,   # kept for call-site compatibility; each Gemini call
                          # has its own SDK-level timeout, there's no external
                          # process to kill anymore
    dataset_id: "str | None" = None,   # optional — used only for the log filename
) -> tuple[str, str, int]:
    """Extract the last observation date from one or more downloaded files.

    Returns (last_obs_date, column_used, files_checked) — same shape as the
    old pi-coding-agent version, so existing callers don't need to change
    beyond the import name.
    """
    files = [filepath] if isinstance(filepath, str) else list(filepath)

    dataset = dataset_id or (
        os.path.basename(os.path.dirname(os.path.abspath(files[0]))) if files else "unknown"
    )
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs(LOGS_DIR, exist_ok=True)
    log_path = os.path.join(LOGS_DIR, f"{dataset}_{ts}.log")

    with open(log_path, "w") as f:
        def log_fn(msg: str) -> None:
            f.write(msg + "\n")
            f.flush()

        log_fn(f"=== file date extraction log ===\nfiles: {files}\nground_truth: {ground_truth}\n")

        per_file: list[tuple[str, str]] = []
        for fp in files:
            log_fn(f"\n{'='*40}\nFile: {fp}\n{'='*40}")
            date, col = _extract_one_file(fp, ground_truth, max_retries, log_fn)
            per_file.append((date, col))

        valid = [(d, c) for d, c in per_file if d and d != "not_possible"]
        if not valid:
            log_fn("\nNo file produced a usable date.")
            print(f"  [log] {log_path}")
            return "not_possible", "none", 0

        best_date, best_col = max(valid, key=lambda dc: dc[0])
        log_fn(f"\nFinal: last_obs_date={best_date} column={best_col} files_checked={len(files)}")

    print(f"  [log] {log_path}")
    return best_date, best_col, len(files)
