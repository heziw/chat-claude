# retrieval-orchestrator: FastAPI, dense-only retrieval + streamed generation (phase 1).
# See shared/CONTRACTS.md for API contracts and env vars.
FROM python:3.11-slim

WORKDIR /app

COPY services/retrieval-orchestrator/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY services/retrieval-orchestrator/app ./app

ENV PYTHONUNBUFFERED=1

EXPOSE 8001

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
