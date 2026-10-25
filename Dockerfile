# syntax=docker/dockerfile:1
ARG PYTHON_VERSION=3.14
ARG ALPINE_VERSION=3.23

# ===== Stage 1: Python dependencies =====
FROM python:${PYTHON_VERSION}-alpine${ALPINE_VERSION} AS python-builder

WORKDIR /build
RUN apk add --no-cache build-base libffi-dev jpeg-dev zlib-dev libxml2-dev libxslt-dev

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ===== Stage 2: final image =====
FROM python:${PYTHON_VERSION}-alpine${ALPINE_VERSION}

RUN addgroup -g 1000 app && adduser -D -u 1000 -G app -h /app -s /sbin/nologin app

RUN apk add --no-cache chromium chromium-chromedriver setpriv \
        libffi libjpeg-turbo zlib libxml2 libxslt libstdc++ tzdata font-dejavu

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
