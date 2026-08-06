# Cloud Run Job image for the Gemini-only staleness pipeline
# (staleness_pipeline_v2.py — Tier 0/1/2 cascade: domain handlers, Gemini
# computer-use, and a download+file-inspection hand-off; BigQuery in/out).

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium --with-deps

COPY *.py ./

CMD ["python3", "staleness_pipeline_v2.py"]
