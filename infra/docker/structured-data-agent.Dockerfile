# structured-data-agent: FastAPI, NL->pandas queries over the XLSX/CSV/JSON
# parquet side-store (phase 1). See shared/CONTRACTS.md for API contracts
# and env vars.
FROM python:3.11-slim

WORKDIR /app

COPY services/structured-data-agent/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY services/structured-data-agent/app ./app

ENV PYTHONUNBUFFERED=1

EXPOSE 8002

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8002"]
