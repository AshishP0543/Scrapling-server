FROM python:3.10-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SCRAPLING_DASH_HOST=0.0.0.0 \
    SCRAPLING_API_HOST=0.0.0.0 \
    SCRAPLING_DASH_PORT=8770 \
    SCRAPLING_API_PORT=8771

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md MANIFEST.in setup.cfg ./
COPY scrapling ./scrapling

RUN pip install --upgrade pip \
    && pip install ".[fetchers]" markdownify \
    && python -m playwright install --with-deps chromium

COPY dashboard ./dashboard
COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

RUN useradd --create-home --shell /bin/bash app \
    && chown -R app:app /app /home/app
USER app

EXPOSE 8770 8771

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
CMD ["api"]
