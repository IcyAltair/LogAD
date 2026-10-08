FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt
COPY generator.py .

# Runs as root on purpose (non-PROD stand): it writes ground truth into a
# host bind mount, and this avoids UID/permission mismatches on Linux hosts.
CMD ["python", "generator.py"]