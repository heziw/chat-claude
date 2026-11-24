# ingestion-service: FastAPI + Celery worker, PDF ingestion (phase 1).
# See shared/CONTRACTS.md for API contracts and env vars.
FROM python:3.13-slim

# tesseract-ocr is installed as a base dependency for the OCR fallback path
# (scanned PDF pages). heb+eng language packs cover current needs. No DB
# client system packages needed: python-tds (the MSSQL driver) is pure
# Python, unlike psycopg2 which this used to require libpq-dev/gcc for.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-heb \
        tesseract-ocr-eng \
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
