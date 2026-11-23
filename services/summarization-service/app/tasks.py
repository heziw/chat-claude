"""Celery app + the hierarchical summarization task.

Broker and result backend are both REDIS_URL, per CONTRACTS.md -- same
pattern as ingestion-service/app/tasks.py. Unlike ingestion-service, we
don't need a separate task_id -> file_id Redis key: the GET /summarize/{task_id}
endpoint only ever needs {status, summary}, both of which live directly in
the Celery result (stored in the Redis backend) keyed by task_id.
"""
from __future__ import annotations

import logging

from celery import Celery

from app import db, milvus_reader
from app.config import settings
from app.summarizer import hierarchical_summarize, judge_summary

logger = logging.getLogger(__name__)

celery_app = Celery(
    "summarization_service",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    # Every service on this REDIS_URL shares one Redis instance/db (see
    # CONTRACTS.md). Celery's default queue name ("celery") is the same for
    # every app unless overridden, so two services' workers would otherwise
    # steal each other's tasks when both run concurrently against the same
    # broker (observed in practice against ingestion-service). Give each
    # service its own default queue.
    task_default_queue="summarization",
)


@celery_app.task(name="summarization.summarize_file", bind=True)
def summarize_file_task(
    self,
    file_id: str,
    thinking_level: str = "standard",
    question: str | None = None,
    system_prompt: str | None = None,
):
    """Map-reduce summarize every Milvus chunk belonging to file_id.

    Steps:
      1. Look up conversation_id / milvus_partition for file_id in MSSQL
         file_metadata (written by ingestion-service).
      2. Fetch all chunks for file_id from that partition in doc_chunks,
         ordered by their true numeric chunk index.
      3. Map: summarize batches of chunks independently, centered on
         `question`'s focus if it has one (see summarizer.py:_focus_text).
      4. Reduce: recursively merge the batch summaries into one final
         summary (capped recursion depth).
      5. When thinking_level=="deep": judge_summary() checks the result;
         on an unsatisfactory verdict, re-run steps 3-4 exactly once with
         the judge's reason folded into the map instruction. One judge
         call, at most one full re-run -- never an open loop (same bounded
         shape as structured-data-agent's judge+replan).

    Raises (and lets Celery mark FAILURE) if the file isn't indexed yet or
    has no chunks -- the caller sees this via GET /summarize/{task_id}.
    """
    fm = db.get_file_metadata(file_id)
    if fm is None:
        raise ValueError(f"No file_metadata row found for file_id={file_id}")

    partition_name = fm.get("milvus_partition")
    if not partition_name:
        raise ValueError(
            f"file_id={file_id} has no milvus_partition yet "
            f"(index_status={fm.get('index_status')!r}); file may still be indexing"
        )

    chunks = milvus_reader.fetch_chunks_for_file(partition_name, file_id)
    if not chunks:
        raise ValueError(f"No chunks found in Milvus for file_id={file_id} (partition={partition_name})")

    logger.info("summarize_file_task: file_id=%s, %d chunk(s) fetched from partition=%s",
                file_id, len(chunks), partition_name)

    summary = hierarchical_summarize(
        chunks, thinking_level=thinking_level, question=question, system_prompt=system_prompt
    )

    judge: dict[str, object] | None = None
    if thinking_level == "deep":
        chunk_texts = [c["text"] for c in chunks if c.get("text")]
        judge = judge_summary(question, chunk_texts, summary, thinking_level)
        if not judge.get("satisfactory", True):
            logger.info(
                "summarize_file_task: judge unsatisfied for file_id=%s (%s); re-running once",
                file_id,
                judge.get("reason"),
            )
            replan_summary = hierarchical_summarize(
                chunks,
                thinking_level=thinking_level,
                question=question,
                extra_context=str(judge.get("reason") or ""),
                system_prompt=system_prompt,
            )
            if replan_summary.strip():
                summary = replan_summary
                judge["corrected"] = True
            else:
                judge["corrected"] = False
        else:
            judge["corrected"] = None

    result: dict[str, object] = {"file_id": file_id, "summary": summary, "chunk_count": len(chunks)}
    if judge is not None:
        result["judge"] = judge
    return result
