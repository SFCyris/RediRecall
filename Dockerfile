# syntax=docker/dockerfile:1
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# RediRecall container. Pairs with a Redis 8 service (see docker-compose.yml),
# which provides the Query Engine (search) that redisvl needs. Multi-arch build:
#   docker buildx build --platform linux/amd64,linux/arm64 -t ghcr.io/sfcyris/redirecall .
FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="RediRecall" \
      org.opencontainers.image.description="Self-hosted retrieval-augmented chat backed by Redis vector search" \
      org.opencontainers.image.source="https://github.com/SFCyris/RediRecall" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    REDIRECALL_DATA_DIR=/data \
    REDIRECALL_HOST=0.0.0.0 \
    REDIRECALL_PORT=8420

# Runtime deps: curl (HEALTHCHECK), tini (PID 1 / signal + zombie reaping).
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

# Install the app + its dependencies. build-essential covers any dependency
# without a prebuilt wheel and is purged in the same layer to keep the image lean.
#
# torch is installed FIRST from PyTorch's CPU-only index, deliberately. The default
# PyPI torch declares NVIDIA CUDA runtime wheels (cuda-bindings, nvidia-cudnn-*,
# nvidia-cusparselt-*, …) that are PROPRIETARY ("LicenseRef-NVIDIA-SOFTWARE-LICENSE").
# Redistributing those inside this AGPL-3.0 image would combine proprietary binaries
# with copyleft code, so they must not be pulled in. Installing the +cpu build up
# front satisfies sentence-transformers' torch requirement, so the following
# `pip install .` will not re-resolve it. It also cuts the image by ~2 GB.
# RediRecall runs embeddings on CPU in the container; for GPU, build your own image.
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && pip install --upgrade pip \
    && pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch \
    && pip install --no-cache-dir . \
    && apt-get purge -y --auto-remove build-essential \
    && rm -rf /var/lib/apt/lists/*

# Run as a non-root user. A write-path bug or container escape otherwise acts as
# uid 0, and every file written into the mounted /data is root-owned on the host.
# This MUST precede VOLUME: filesystem changes to a declared volume path are
# discarded from the image layer, so a chown after it silently does nothing.
RUN useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin redirecall \
    && mkdir -p /data && chown -R redirecall:redirecall /data /app

# Config, uploads, logs, ingestion history — back this up.
VOLUME ["/data"]
USER redirecall
EXPOSE 8420

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8420/api/health || exit 1

ENTRYPOINT ["tini", "--"]
CMD ["redirecall"]
