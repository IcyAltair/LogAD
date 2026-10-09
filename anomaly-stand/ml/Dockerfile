FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/srv/src \
    MPLBACKEND=Agg

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY src ./src
COPY tests ./tests

# Root on purpose (non-PROD): writes models/reports into a host bind mount.
EXPOSE 9200
CMD ["uvicorn", "anomaly.detector:app", "--host", "0.0.0.0", "--port", "9200"]