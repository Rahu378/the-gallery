# The Gallery — container image for Cloud Run.
#
# Two stages: the builder installs dependencies and warms the FastF1 cache so
# the race travels inside the image; the runtime carries only the virtualenv,
# the cache and the app.

# ─────────────────────────────── builder ───────────────────────────────
FROM python:3.13-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY requirements.txt .
RUN pip install -r requirements.txt

# Ship the cache from the build context first. Cloud Build egress cannot reach
# the F1 timing API — position data comes back empty there — so regenerating
# the race inside the build silently produces an empty cache and the container
# falls back to the synthetic source. A locally warmed cache always wins;
# prefetch below only fills a gap when the context has none.
COPY cache/ /build/cache/
COPY scripts/prefetch.py scripts/prefetch.py
ARG RACE_YEAR=2023
ARG RACE_EVENT=Monza
ARG RACE_SESSION=R
ENV RACE_YEAR=${RACE_YEAR} RACE_EVENT=${RACE_EVENT} RACE_SESSION=${RACE_SESSION}
RUN python scripts/prefetch.py

# ─────────────────────────────── runtime ───────────────────────────────
FROM python:3.13-slim

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --from=builder /build/cache /app/cache
COPY backend/ backend/
COPY frontend/ frontend/
COPY scripts/ scripts/

# Cloud Run runs containers as an arbitrary UID; keep the app dir readable and
# the cache writable so FastF1 can top it up for any race not baked in.
RUN useradd --create-home --uid 1001 gallery \
    && chown -R gallery:gallery /app
USER gallery

# Cloud Run injects PORT. Shell form so it expands; exec so SIGTERM reaches
# uvicorn and in-flight WebSockets close cleanly on revision swap.
ENV PORT=8080
EXPOSE 8080
CMD exec uvicorn backend.main:app --host 0.0.0.0 --port ${PORT} --no-access-log
