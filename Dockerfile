# Polyglot TTS — multi-language streaming TTS server with voice cloning.
#
# Two image variants are built from this Dockerfile via build-arg PYTORCH_INDEX:
#   - CPU (default)  : ghcr.io/nosdave/polyglot-tts:latest  (linux/amd64 + linux/arm64)
#   - CUDA           : ghcr.io/nosdave/polyglot-tts:cuda    (linux/amd64 + linux/arm64-cuda13)
#
# Build locally:
#   docker build -t polyglot-tts:local .
#   docker build --build-arg PYTORCH_INDEX=cu128 -t polyglot-tts:cuda-local .

ARG PYTORCH_INDEX=cpu

# ============================================================
# BUILDER STAGE
# ============================================================
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder
ARG PYTORCH_INDEX

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Build deps for audio libs and any source-wheel fallbacks
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libportaudio2 \
    portaudio19-dev \
    libsndfile1 \
    libsndfile1-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV UV_SYSTEM_PYTHON=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_HTTP_TIMEOUT=600 \
    UV_CONCURRENT_DOWNLOADS=4

# Step 1: install torch from the appropriate index (cpu or cu128)
#   - cpu  index: https://download.pytorch.org/whl/cpu     (multi-arch, small)
#   - cu128 index: https://download.pytorch.org/whl/cu128  (amd64 only on PyPI;
#     for arm64+cuda13 the Spark variant uses NVIDIA's nightly wheel — adjust
#     PYTORCH_INDEX at build-time to point at an arm-cuda wheel index if needed)
COPY pyproject.toml .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install torch --index-url "https://download.pytorch.org/whl/${PYTORCH_INDEX}" && \
    uv pip install -r pyproject.toml

# Step 2: install the package itself (non-editable)
COPY polyglot_tts/ polyglot_tts/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --no-deps .

# Step 3: trim site-packages — strip docs/tests/headers we don't need at runtime.
# Verified safe in production: pocket_tts loads fine without these.
RUN rm -rf /usr/local/lib/python3.13/site-packages/sympy \
           /usr/local/lib/python3.13/site-packages/sympy-*.dist-info \
           /usr/local/lib/python3.13/site-packages/networkx \
           /usr/local/lib/python3.13/site-packages/networkx-*.dist-info \
           /usr/local/lib/python3.13/site-packages/pygments \
           /usr/local/lib/python3.13/site-packages/Pygments-*.dist-info \
           /usr/local/lib/python3.13/site-packages/pip \
           /usr/local/lib/python3.13/site-packages/pip-*.dist-info \
           /usr/local/lib/python3.13/site-packages/setuptools \
           /usr/local/lib/python3.13/site-packages/setuptools-*.dist-info \
           /usr/local/lib/python3.13/site-packages/torch/include \
           /usr/local/lib/python3.13/site-packages/torch/share \
           /usr/local/lib/python3.13/site-packages/torch/_inductor \
           /usr/local/lib/python3.13/site-packages/caffe2 \
    && find /usr/local/lib/python3.13/site-packages -type d -name "tests" -exec rm -rf {} + 2>/dev/null || true \
    && find /usr/local/lib/python3.13/site-packages -type d -name "test"  -exec rm -rf {} + 2>/dev/null || true \
    && find /usr/local/lib/python3.13/site-packages -type f -name "*.pyi" -delete 2>/dev/null || true \
    && find /usr/local/lib/python3.13/site-packages -type f -name "*.pyx" -delete 2>/dev/null || true \
    && find /usr/local/lib/python3.13/site-packages -type f -name "*.c"   -delete 2>/dev/null || true \
    && find /usr/local/lib/python3.13/site-packages -type f -name "*.h"   -delete 2>/dev/null || true

# ============================================================
# RUNTIME STAGE
# ============================================================
FROM python:3.13-slim-bookworm

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update && apt-get install -y --no-install-recommends \
    libportaudio2 \
    libsndfile1 \
    netcat-openbsd \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /var/cache/apt/*

COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin/polyglot-tts /usr/local/bin/

# Run as a non-root user (UID 10001). Owners of mounted volumes (voices-extra,
# hf-cache) must allow read+write to UID 10001 — see docs/CONFIGURATION.md.
#
# IMPORTANT: pre-create the FULL mount paths (/app/.cache/huggingface and
# /app/voices-extra) owned by polyglot. When Docker mounts an empty named
# volume onto a path that already exists in the image, it initializes the
# volume with that path's ownership. If the path didn't pre-exist, Docker
# creates the mountpoint as root and the non-root process gets EACCES.
RUN useradd --system --uid 10001 --shell /usr/sbin/nologin --home-dir /app polyglot \
    && mkdir -p /app/voices /app/voices-extra /app/.cache/huggingface \
    && chown -R polyglot:polyglot /app

ENV HOME=/app \
    HF_HOME=/app/.cache/huggingface

WORKDIR /app
USER polyglot

# Ports:
#   10200 — Wyoming protocol (Home Assistant, Rhasspy)
#   10201 — OpenAI-Speech-compatible HTTP (OpenClaw, scripts, anything)
#   10299 — side-channel timing endpoint (sparkdash-style observability)
EXPOSE 10200 10201 10299

# Health check on whichever endpoint is enabled — try HTTP first (cheapest)
HEALTHCHECK --interval=30s --timeout=10s --start-period=300s --retries=3 \
    CMD curl -fsS "http://localhost:${POCKET_TTS_HTTP_PORT:-10201}/health" >/dev/null 2>&1 \
        || echo '{"type":"describe"}' | nc -w 5 localhost "${POCKET_TTS_WYOMING_PORT:-10200}" | grep -q "pocket-tts" \
        || exit 1

# Default env values shipped with the image
ENV POCKET_TTS_LANGUAGES=english_2026-04,german_24l,french_24l \
    POCKET_TTS_VOICE=eve \
    POCKET_TTS_WYOMING_PORT=10200 \
    POCKET_TTS_HTTP_PORT=10201 \
    POCKET_TTS_TIMING_PORT=10299 \
    POCKET_TTS_DEVICE=auto \
    POCKET_TTS_WARMUP=true \
    POCKET_TTS_TEXT_NORM=true \
    POCKET_TTS_AUTO_LID=true \
    POCKET_TTS_VOICES_DIR=/app/voices \
    POCKET_TTS_VOICES_EXTRA_DIR=/app/voices-extra

# Entrypoint runs the dispatcher (which starts every enabled endpoint).
CMD ["python", "-m", "polyglot_tts"]
