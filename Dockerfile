# Lanloom - read-only circuit-board map of the local network.
# Multi-arch (amd64, arm64). Runs as an unprivileged user; only /data is written.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANLOOM_DATA=/data \
    LANLOOM_PORT=7380 \
    HOME=/tmp

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && groupadd --system --gid 10001 lanloom \
 && useradd --system --uid 10001 --gid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin lanloom \
 && mkdir -p /data && chown 10001:10001 /data && chmod 0750 /data

COPY VERSION LICENSE THIRD-PARTY-NOTICES.md ./
COPY lanloom ./lanloom
COPY updater ./updater
# The version argument comes late so a new version does not invalidate the dependency layer above.
ARG VERSION=0.0.0
RUN python -m compileall -q lanloom updater && chmod -R a+rX /app

LABEL org.opencontainers.image.title="lanloom" \
      org.opencontainers.image.description="Read-only circuit-board map of your local network" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.licenses="LicenseRef-Lanloom-Source-Available" \
      org.opencontainers.image.source="https://github.com/robeertm/lanloom"

USER 10001:10001
VOLUME ["/data"]
EXPOSE 7380
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,sys,urllib.request; p=os.environ.get('LANLOOM_PORT','7380'); sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/api/health' % p, timeout=4).status == 200 else 1)"
CMD ["python", "-m", "lanloom"]
