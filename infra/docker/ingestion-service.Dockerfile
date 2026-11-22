# ingestion-service: FastAPI + Celery worker, PDF ingestion (phase 1).
# See shared/CONTRACTS.md for API contracts and env vars.
FROM python:3.11-slim

# tesseract-ocr is installed as a base dependency for the OCR fallback path
# (scanned PDF pages). heb+eng language packs cover current needs; other
# file-type parsers added later (docx/xlsx/csv/json/image) may need
# additional system packages - add them here when that work lands.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-heb \
        tesseract-ocr-eng \
        libpq-dev \
        gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY services/ingestion-service/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY services/ingestion-service/app ./app

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

# Default command runs the FastAPI app. The Celery worker should be run as
# a separate container/process from the same image, e.g.:
#   celery -A app.tasks.celery_app worker --loglevel=info
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
