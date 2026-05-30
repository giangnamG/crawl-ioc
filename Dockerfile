FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    FLASK_ENV=production \
    FLASK_DEBUG=0 \
    IOC_DB_PATH=/data/ioc_investigator.sqlite3 \
    CLOAK_HEADLESS=1 \
    BROWSER_PROVIDER=cloak \
    CLOAK_PROFILE_ROOT=/data/cloak_profiles \
    WORKER_POLL_SECONDS=3 \
    SEARCH_FAST_MODE=true \
    SEARCH_TYPE_DELAY_MIN=2 \
    SEARCH_TYPE_DELAY_MAX=5 \
    SEARCH_PAGE_DELAY_MIN=0 \
    SEARCH_PAGE_DELAY_MAX=0

# Install minimal system dependencies required by headless browsers
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl wget gnupg \
        libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libx11-xcb1 libxcomposite1 libxrandr2 libxss1 libgconf-2-4 libasound2 libgbm1 fonts-liberation tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

# Install CloakBrowser browsers (if cloakbrowser is present this will download runtime browsers)
RUN python -m cloakbrowser install || true

# Expose data directory as a mount so the SQLite DB can be persisted on the host
VOLUME ["/data"]
EXPOSE 5000

# Use a single Gunicorn worker (SQLite + concurrency) and bind to all interfaces
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "4", "app:app"]
