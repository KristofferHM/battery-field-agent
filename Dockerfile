# Ingest worker: normalises one release into canonical Parquet.
# Multi-stage so the runtime layer stays small and cacheable.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# --- deps layer (cached independently of source changes) -------------------
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# --- source layer ----------------------------------------------------------
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir --no-deps -e .

# Non-root: Kaggle and CI both run unprivileged; match that so permission
# surprises surface in CI rather than in a notebook.
RUN useradd --create-home --uid 1000 worker \
 && mkdir -p /data /out \
 && chown -R worker:worker /data /out /app
USER worker

ENV BFA_DATA_DIR=/data \
    BFA_OUT_DIR=/out

# Default: show the catalogue. Overridden by the workflow.
ENTRYPOINT ["python", "-m", "battery_field_agent.cli"]
CMD ["list"]
