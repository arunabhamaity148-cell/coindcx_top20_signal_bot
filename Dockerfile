# ============================================================================
# CoinDCX TOP-20 SIGNAL-ONLY FUTURES INTELLIGENCE BOT
# Multi-stage, non-root, read-only-filesystem compatible image.
#
# The container has NO ability to trade: this codebase contains no order
# placement, cancellation, modification, close, leverage or transfer call, and
# the boot assertion in app/safety.py aborts the process if an exchange trading
# credential is present in the environment.
# ============================================================================
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ---- build stage: compile wheels once -------------------------------------
FROM base AS builder

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -r requirements.txt

# ---- runtime stage --------------------------------------------------------
FROM base AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    SIGNAL_BOT_MODE=signal_only \
    CONFIG_DIR=/app/config \
    LOG_DIR=/app/logs

COPY --from=builder /opt/venv /opt/venv
COPY app ./app
COPY config ./config
COPY scripts ./scripts
COPY docs ./docs
COPY pyproject.toml README.md LICENSE .env.example ./

RUN useradd --create-home --uid 10001 signalbot \
 && mkdir -p /app/data /app/logs \
 && chown -R signalbot:signalbot /app

USER signalbot

# Health probe: configuration + safety invariants + journal writability.
HEALTHCHECK --interval=5m --timeout=30s --start-period=60s --retries=3 \
  CMD ["python", "scripts/healthcheck.py", "--quiet"]

# Default: startup safety checks, then the signal loop (dry-run Telegram).
CMD ["python", "-m", "app.main"]
