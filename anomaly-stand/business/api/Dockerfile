FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# Dependencies first for better layer caching
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# Run as non-root (the app writes nothing to disk)
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

# --workers 1: chaos state is in-process memory; async handles concurrency.
# --no-access-log: nginx is the single source of truth for request logs.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--no-access-log", "--proxy-headers", "--forwarded-allow-ips", "*"]