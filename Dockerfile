# Cloud Run Job image for the Gemini-only staleness pipeline
# (staleness_pipeline_v2.py — Tier 0/1/2 cascade: domain handlers, Gemini
# computer-use, and a download+pi-coding-agent hand-off; BigQuery in/out).

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ── Install pi CLI (AI coding agent — @earendil-works/pi-coding-agent) ─────────
# Needed by Tier 2 (computer_use_extractor.tier1_computer_use's download
# hand-off -> pi_date_extractor.extract_date_with_pi()). Auth is non-interactive:
# the pi CLI reads GEMINI_API_KEY straight from the environment (confirmed by
# a live smoke test), which cloudbuild.yaml already injects as a secret — no
# separate `pi` login/config step needed in this image.
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g @earendil-works/pi-coding-agent

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium --with-deps

COPY *.py ./

CMD ["python3", "staleness_pipeline_v2.py"]
