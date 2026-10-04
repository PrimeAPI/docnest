# syntax=docker/dockerfile:1.7
# DocNest — multi-stage build.
#   frontend : builds the React app (Node only exists in this stage)
#   base     : runtime OS packages (OCR toolchain, Proton Drive CLI)
#   dev      : base + dev dependencies, used for tests/CI
#   runtime  : production image

ARG PYTHON_VERSION=3.13

# ---------------------------------------------------------------------------
FROM node:24-slim AS frontend
ENV PNPM_HOME=/pnpm PATH=/pnpm:$PATH CI=true
RUN corepack enable
WORKDIR /src/frontend
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-trixie AS base

ARG PROTON_DRIVE_CLI_VERSION=0.8.0
ARG PROTON_DRIVE_CLI_SHA256=9443d771719c892790db17e6f02ecd99ad7c53593329f3a67c77677dce577735

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl \
      tesseract-ocr tesseract-ocr-deu tesseract-ocr-eng \
      ghostscript poppler-utils unpaper pngquant qpdf \
      libpq5 util-linux \
 && curl -fsSL -o /tmp/proton-drive \
      "https://proton.me/download/drive/cli/${PROTON_DRIVE_CLI_VERSION}/linux-x64/proton-drive" \
 && echo "${PROTON_DRIVE_CLI_SHA256}  /tmp/proton-drive" | sha256sum -c - \
 && install -D -m 0755 /tmp/proton-drive /opt/proton-drive/proton-drive \
 && rm /tmp/proton-drive \
 && apt-get purge -y curl && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

RUN pip install "uv>=0.5"

RUN groupadd --gid 10001 docnest \
 && useradd --uid 10001 --gid docnest --home-dir /var/lib/docnest --create-home --shell /usr/sbin/nologin docnest

# Proton Drive CLI integration: `pass` is DocNest's encrypted credentials shim.
COPY --chmod=0755 docker/proton/docnest-pass /usr/local/bin/pass
COPY --chmod=0755 docker/proton/docnest-proton /usr/local/bin/docnest-proton
# The Proton Drive CLI is MIT-licensed (github.com/ProtonDriveApps/sdk); its license ships with the binary.
COPY docker/proton/LICENSE.proton-drive-cli.md /opt/proton-drive/LICENSE.md

ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PROTON_DRIVE_CREDENTIALS_STORE=pass \
    PROTON_DRIVE_CACHE_DIR=/var/lib/docnest/proton/cache \
    PROTON_DRIVE_LOG_LEVEL=WARNING \
    DOCNEST_PROTON_SESSION_DIR=/var/lib/docnest/proton/session

WORKDIR /app/backend

# ---------------------------------------------------------------------------
FROM base AS dev
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-install-project
ENV DOCNEST_DEV=true
CMD ["bash"]

# ---------------------------------------------------------------------------
FROM base AS runtime-deps
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# ---------------------------------------------------------------------------
FROM base AS runtime
COPY --from=runtime-deps /opt/venv /opt/venv
RUN pip uninstall -y uv
COPY backend/ /app/backend/
COPY --from=frontend /src/frontend/dist /app/frontend/dist
COPY --chmod=0755 docker/entrypoint.sh /usr/local/bin/docnest
COPY docker/supervisord.conf /etc/docnest/supervisord.conf

RUN python -m compileall -q /app/backend \
 && install -d -o docnest -g docnest -m 0700 /var/lib/docnest/data /var/lib/docnest/proton

ENV DOCNEST_DATA_DIR=/var/lib/docnest/data \
    DOCNEST_FRONTEND_DIR=/app/frontend/dist \
    DOCNEST_MASTER_KEY_FILE=/run/secrets/master_key \
    DOCNEST_SECRET_KEY_FILE=/run/secrets/django_secret_key \
    DOCNEST_DB_PASSWORD_FILE=/run/secrets/db_password \
    DOCNEST_WORK_DIR=/tmp/docnest-work \
    HOME=/var/lib/docnest

ARG VERSION=dev
ENV DOCNEST_VERSION=${VERSION}
LABEL org.opencontainers.image.source="https://github.com/PrimeAPI/docnest" \
      org.opencontainers.image.description="Secure self-hosted document management" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${VERSION}"

USER docnest
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4).status == 200 else 1)"]
ENTRYPOINT ["docnest"]
CMD ["all"]
