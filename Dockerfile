FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && pip install . \
 && useradd --system --uid 10001 --no-create-home app \
 && mkdir -p /data && chown app /data

# Runs as uid 10001 by default; `user: "$PUID:$PGID"` in compose works too,
# as long as the /data volume belongs to that user.

USER app
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; \
r=urllib.request.urlopen('http://127.0.0.1:8000/.well-known/oauth-authorization-server', timeout=4); \
sys.exit(0 if r.status==200 else 1)"

CMD ["insta-mcp"]
