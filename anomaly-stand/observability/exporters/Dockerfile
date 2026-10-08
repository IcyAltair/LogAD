# One small image for both Python exporters (nginx-log-exporter, gt-annotator).
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY nginx_log_exporter.py gt_annotator.py ./
# Runs as root on purpose: nginx may create logs with restrictive permissions
# and this is a non-PROD stand. Mounts are read-only anyway.
CMD ["python", "nginx_log_exporter.py"]
