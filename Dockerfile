# Multi-stage build: one image serves both Render service types (see
# render.yaml) -- the Web Service runs the default CMD (uvicorn), the
# Background Worker overrides it with `python scripts/run_worker.py`.
# Neither needs the dhanhq extra installed to build; it's optional at
# runtime (broker/dhan_client.py degrades to a clear error, not a crash,
# when it's absent -- see broker/factory.py).

FROM python:3.11-slim AS builder

WORKDIR /build

# Only copies what pip needs to resolve dependencies first, so this layer
# (the slow one -- dozens of packages, including litellm/pandas/numpy)
# stays cached across source-only changes.
COPY pyproject.toml ./
COPY vibetrading/__init__.py vibetrading/__init__.py

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir ".[dhan]"

COPY . .
RUN pip install --no-cache-dir --no-deps -e .


FROM python:3.11-slim AS runtime

RUN groupadd --system app && useradd --system --gid app --create-home app

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1

# WORKDIR creates /app as root; COPY --chown only chowns what it copies
# in, not the directory itself, so a plain COPY --chown here would still
# leave /app root-owned -- the runtime user (below) needs to write there
# too (a local SQLite dev DB, e.g.), so chown it explicitly.
WORKDIR /app
RUN chown app:app /app
COPY --chown=app:app . .

USER app

# Render sets $PORT; default to 8000 for local `docker run`. Migrations
# are deliberately NOT run here -- baking `alembic upgrade head` into the
# container's own startup would race N web replicas against each other on
# every deploy/restart. render.yaml's preDeployCommand runs it exactly
# once, before traffic ever reaches a new instance.
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn vibetrading.api.app:app --host 0.0.0.0 --port ${PORT}"]
