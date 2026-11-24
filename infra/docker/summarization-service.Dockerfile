# summarization-service: FastAPI + Celery worker, hierarchical map-reduce
# summarization over Milvus-indexed document chunks.
# See shared/CONTRACTS.md for API contracts and env vars.
FROM python:3.13-slim

WORKDIR /app

COPY services/summarization-service/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY services/summarization-service/app ./app

ENV PYTHONUNBUFFERED=1

EXPOSE 8003

# Default command runs the FastAPI app. The Celery worker should be run as
# a separate container/process from the same image, e.g.:
#   celery -A app.tasks.celery_app worker --loglevel=info
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8003"]
