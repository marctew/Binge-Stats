# syntax=docker/dockerfile:1
# Stage 1: fetch pinned assets and build the CSS with the standalone Tailwind CLI.
FROM debian:bookworm-slim AS assets
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY scripts/ scripts/
COPY bingestats/static/ bingestats/static/
COPY bingestats/templates/ bingestats/templates/
RUN scripts/fetch-assets.sh && scripts/fetch-assets.sh tailwind && scripts/build-css.sh

# Stage 2: the app.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data PUID=1000 PGID=1000
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bingestats/ bingestats/
COPY --from=assets /src/bingestats/static/css/app.css bingestats/static/css/app.css
COPY --from=assets /src/bingestats/static/vendor/ bingestats/static/vendor/
COPY docker/entrypoint.sh /entrypoint.sh
ARG GIT_SHA=""
ENV GIT_SHA=${GIT_SHA}
VOLUME /data
EXPOSE 8181
ENTRYPOINT ["/entrypoint.sh"]
CMD ["web"]
