FROM python:3.12-slim

# Non-root user; /data holds the SQLite DB and the downloaded FIT files
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /data && chown appuser:appuser /data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

USER appuser

EXPOSE 8080

# --proxy-headers + --forwarded-allow-ips: trust X-Forwarded-* from the
# reverse proxy (Caddy) so request.url / redirects use the public scheme/host.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
