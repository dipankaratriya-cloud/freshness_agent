"""Phase 4: pi coding agent (Gemini) finds the last observation date.

Accepts a single file path OR a list of file paths.
For multi-file datasets the agent inspects every file, extracts the per-file
max date, and returns the global maximum.
"""

import json
import os
import re
import subprocess
import threading
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

LOGS_DIR = os.path.join(os.path.dirname(__file__), "logs")

# ─── Prompts ──────────────────────────────────────────────────────────────────

_SINGLE_FILE_PROMPT = """\
Dataset file: {filepath}

Task: Find the LAST OBSERVATION DATE — the year/date the data DESCRIBES, not when it was published or sourced.

## Column priority rules (follow strictly)
1. PREFER columns named: Year, Date, Period, Observation_Date, Ref_Date, Reference_Period, Time_Period
2. IGNORE columns named: Source_Year, Source Year, Publication_Year, Reported_Year, Data_Year, Access_Date
3. If both "Year" and "Source Year" exist → use ONLY "Year"

## Known patterns
- Wide format: year values are column headers (e.g. 2018, 2019, 2020) → last numeric header is the answer
- Long format: a single date/year column with repeated values → max of that column
- Split format: year + month in separate columns → combine as YYYY-MM, take max
- Large file (>100 MB): use nrows=1000 for initial inspection, then nrows=None only if needed

## Steps
1. Run bash: head -3 and column inspection to identify date columns
2. Apply column priority rules to pick the correct column
3. Write + run a Python script:  max(df[col])  — use nrows=1000 for files >100MB
4. Verify: year must be 1900–2030; if not, re-examine and retry
5. Iterate until valid result or all options exhausted

Output ONLY this JSON on its own line (no markdown):
{{"last_obs_date": "YYYY-MM-DD or YYYY", "column_used": "column_name_or_strategy", "files_checked": 1}}

If truly not possible:
{{"last_obs_date": "not_possible", "column_used": "none", "files_checked": 0}}"""


_MULTI_FILE_PROMPT = """\
Dataset files ({n} files — check ALL of them):
{file_list}

Task: Find the LAST OBSERVATION DATE across ALL files above — the single most recent
date any of these files contains data for. Dates from file names/paths are hints only;
always verify by reading the actual data.

## Column priority rules (follow strictly)
1. PREFER columns named: Year, Date, Period, Observation_Date, Ref_Date, Reference_Period, Time_Period
2. IGNORE columns named: Source_Year, Source Year, Publication_Year, Reported_Year, Data_Year, Access_Date
3. If both "Year" and "Source Year" exist → use ONLY "Year"

## Known patterns
- Wide format: year values are column headers → last numeric header is the date
- Long format: a date/year column with repeated values → max of that column
- Split format: year + month in separate columns → combine, take max
- Large file (>100 MB): use nrows=1000 for inspection — DO NOT read the full file

## Steps — repeat for EVERY file
1. `ls -lh <file>` to check size before opening
2. `head -3 <file>` or column inspection to identify the date column
3. Write + run a one-liner to get that file's max date:
     python3 -c "import pandas as pd; df=pd.read_csv('<f>', nrows=1000); print(df['Year'].max())"
   (adjust read function for .xlsx/.tsv/.parquet etc.; use nrows=1000 for files >50MB)
4. Record: file → max_date
5. Move to the next file

## After checking ALL files
- global_max = max of all per-file max dates
- That is the last_obs_date to report
- files_checked = how many files you actually read

Output ONLY this JSON on its own line (no markdown):
{{"last_obs_date": "YYYY-MM-DD or YYYY", "column_used": "column_name_or_strategy", "files_checked": {n}}}

If truly not possible across all files:
{{"last_obs_date": "not_possible", "column_used": "none", "files_checked": 0}}"""


_CORRECTION_PROMPT = """\
Your answer of "{wrong_date}" (from column "{wrong_col}") is incorrect.

The correct answer must come from a column representing the OBSERVATION PERIOD —
when the measurement was taken — not when the data was sourced or published.

Common mistake: choosing "Source Year" or a metadata column over "Year" / "Date".

Re-examine ALL files carefully:
- Look at ALL column names again across all files
- Pick the column representing what time period the DATA DESCRIBES
- Make sure you checked every file — the most recent date may be in a different file
- Run your extraction script and return the corrected result

Output ONLY this JSON on its own line:
{{"last_obs_date": "YYYY-MM-DD or YYYY", "column_used": "column_name_or_strategy", "files_checked": N}}"""


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _parse_result(text: str) -> tuple[str, str, int]:
    """Return (last_obs_date, column_used, files_checked)."""
    m = re.search(r'\{[^}]*"last_obs_date"[^}]*\}', text, re.DOTALL)
    if m:
        try:
            r = json.loads(m.group())
            return (
                r.get("last_obs_date", "not_possible"),
                r.get("column_used", "pi_agent"),
                int(r.get("files_checked", 1)),
            )
        except Exception:
            pass
    return "not_possible", "pi_agent", 0


def _format_event(event: dict) -> str | None:
    etype = event.get("type", "")
    if etype == "agent_start":  return "▶ agent started"
    if etype == "turn_start":   return "── turn start ──"
    if etype == "turn_end":
        return f"── turn end ({len(event.get('toolResults', []))} tool result(s)) ──"
    if etype == "agent_end":
        return f"■ agent_end ({len(event.get('messages', []))} messages total)"
    if etype == "message_update":
        ae = event.get("assistantMessageEvent", {})
        ae_type = ae.get("type", "")
        if ae_type == "text_delta":
            return f"  [text] {ae.get('delta', '')}"
        if ae_type == "tool_start":
            tool = ae.get("toolName") or ae.get("partial", {}).get("name", "")
            return f"  [tool_call] {tool}"
        if ae_type == "tool_input_delta":
            return f"    input: {ae.get('delta', '')}"
        if ae_type == "tool_result":
            content = ae.get("content", "")
            if isinstance(content, list):
                content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
            return f"    result: {str(content)[:300].replace(chr(10), chr(92)+'n')}"
    return None


def _wait_for_agent_end(proc, log, counter: list) -> str:
    """Read pi stdout until agent_end; log events; return final assistant text."""
    final_text = ""
    for line in proc.stdout:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        ae = event.get("assistantMessageEvent", {})
        if event.get("type") == "message_update" and ae.get("type") == "tool_start":
            counter[0] += 1
            log.write(f"\n[tool call #{counter[0]}]\n")

        readable = _format_event(event)
        if readable:
            log.write(readable + "\n")
            log.flush()

        if event.get("type") != "agent_end":
            continue

        for msg in reversed(event.get("messages", [])):
            if msg.get("role") == "assistant":
                for block in msg.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        final_text = block["text"]
                        break
                break
        break

    return final_text


# ─── Public API ───────────────────────────────────────────────────────────────

def extract_date_with_pi(
    filepath: "str | list[str]",
    ground_truth: "str | None" = None,
    max_retries: int = 3,
    timeout: int = 300,
) -> tuple[str, str, int]:
    """Extract the last observation date from one or more data files.

    Args:
        filepath: a single file path OR a list of file paths.
                  When a list is given the agent checks every file and returns
                  the global maximum date across all of them.
        ground_truth: known correct answer (triggers auto-correction on mismatch).
        max_retries: max correction attempts after initial run.
        timeout: seconds before killing the pi agent process.

    Returns:
        (last_obs_date, column_used, files_checked)
    """
    # Normalise to list
    if isinstance(filepath, str):
        files = [filepath]
    else:
        files = list(filepath)

    is_multi = len(files) > 1

    # Working directory = common ancestor of all files
    common_dir = os.path.commonpath([os.path.abspath(f) for f in files])
    if os.path.isfile(common_dir):
        common_dir = os.path.dirname(common_dir)

    # Dataset name for log = name of that common directory
    dataset = os.path.basename(common_dir)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs(LOGS_DIR, exist_ok=True)
    log_path = os.path.join(LOGS_DIR, f"{dataset}_{ts}.log")

    # Build the initial prompt
    if is_multi:
        file_list_str = "\n".join(
            f"  {i+1}. {f}  ({os.path.getsize(f):,} bytes)"
            for i, f in enumerate(files)
        )
        initial_prompt = _MULTI_FILE_PROMPT.format(
            n=len(files),
            file_list=file_list_str,
        )
    else:
        initial_prompt = _SINGLE_FILE_PROMPT.format(filepath=files[0])

    proc = subprocess.Popen(
        ["pi", "--mode", "rpc", "--no-session",
         "--provider", "google", "--model", "gemini-3.1-pro-preview"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True,
        env=os.environ,
        cwd=common_dir,
    )

    counter = [0]
    date, col, n_checked = "not_possible", "pi_agent", 0

    with open(log_path, "w") as log:
        log.write(
            f"=== pi date extraction log ===\n"
            f"dataset : {dataset}\n"
            f"mode    : {'multi-file' if is_multi else 'single-file'} ({len(files)} file(s))\n"
            f"files   : {chr(10).join(files)}\n"
            f"model   : gemini-3.1-pro-preview\n"
            f"started : {datetime.now().isoformat()}\n"
            f"{'='*40}\n\n"
        )

        timer = threading.Timer(timeout, proc.kill)
        try:
            timer.start()

            for attempt in range(1, max_retries + 1):
                if attempt == 1:
                    message = initial_prompt
                else:
                    message = _CORRECTION_PROMPT.format(
                        wrong_date=date, wrong_col=col
                    )

                log.write(f"\n{'─'*40}\n[attempt {attempt}/{max_retries}]\n")
                if attempt > 1:
                    log.write(f"  correction: prev={date} col={col} ground_truth={ground_truth}\n")
                log.write(f"{'─'*40}\n")

                proc.stdin.write(json.dumps({"type": "prompt", "message": message}) + "\n")
                proc.stdin.flush()

                final_text = _wait_for_agent_end(proc, log, counter)
                date, col, n_checked = _parse_result(final_text)

                log.write(f"\n→ attempt {attempt}: last_obs_date={date}  column={col}  files_checked={n_checked}\n")

                if ground_truth is None or str(date) == str(ground_truth):
                    break

                log.write(f"  WRONG — expected {ground_truth}, retrying...\n")

        finally:
            timer.cancel()
            proc.kill()
            proc.wait()

        log.write(
            f"\n{'='*40}\n"
            f"final   : last_obs_date={date}  column={col}  files_checked={n_checked}\n"
            f"ended   : {datetime.now().isoformat()}\n"
        )

    print(f"  [log] {log_path}")
    return date, col, n_checked
