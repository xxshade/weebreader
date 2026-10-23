# syntax=docker/dockerfile:1
ARG PYTHON_VERSION=3.14
ARG DEBIAN_CODENAME=trixie

# ===== Stage 1: Python dependencies =====
FROM python:${PYTHON_VERSION}-slim-${DEBIAN_CODENAME} AS python-builder

WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libffi-dev libjpeg-dev zlib1g-dev libxml2-dev libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ===== Stage 2: final image =====
FROM python:${PYTHON_VERSION}-slim-${DEBIAN_CODENAME}

RUN groupadd --gid 1000 app && useradd --uid 1000 --gid 1000 -d /app -s /sbin/nologin app

RUN apt-get update && apt-get install -y --no-install-recommends chromium chromium-driver \
    && rm -rf /var/lib/apt/lists/*

ENV APP_NOVEL_CHROMIUM_BINARY_PATH=/usr/bin/chromium \
    APP_NOVEL_CHROMEDRIVER_PATH=/usr/bin/chromedriver

WORKDIR /app

COPY --from=python-builder /install /usr/local

COPY app/ /app/app/

RUN mkdir -p /app/data && chown -R app:app /app

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
