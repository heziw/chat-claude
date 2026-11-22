"""Manual/smoke test for the mock-mode /query flow -- no real Milvus needed.

Run with:
    MOCK_LLM=true python test_mock_flow.py

This patches app.retrieval.retrieve so no live Milvus connection is
required, then drives /query through FastAPI's TestClient and prints the
parsed SSE events. It exercises:
  1. the "chunks found" path (citations + mock generation referencing them)
  2. the "no chunks found yet" path (friendly explanatory answer, no crash)

Once Milvus is actually running (e.g. via docker-compose up) this same
service can be hit directly with curl/httpx against a real partition --
this script only stubs retrieval.retrieve, not the Milvus client module
itself, so app/retrieval.py's real Milvus code path is left untouched and
can be exercised separately once there's live data to query.
"""
from __future__ import annotations

import os
import re
from unittest.mock import patch

os.environ.setdefault("MOCK_LLM", "true")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

FAKE_CHUNKS = [
    {
        "chunk_id": "file-123_0",
        "file_id": "file-123",
        "file_type": "pdf",
        "source_ref": "עמוד 3",
        "text": "מדיניות החזרים: ניתן להחזיר מוצר עד 30 יום מתאריך הקנייה, בכפוף לקבלה מקורית.",
        "score": 0.91,
    },
    {
        "chunk_id": "file-123_1",
        "file_id": "file-123",
        "file_type": "pdf",
        "source_ref": "עמוד 4",
        "text": "מוצרים פגומים יוחלפו ללא עלות בכל שלב.",
        "score": 0.85,
    },
]


def _parse_sse(raw_text: str) -> list[tuple[str, str]]:
    events: list[tuple[str, str]] = []
    for block in raw_text.strip().split("\n\n"):
        if not block.strip():
            continue
        event_match = re.search(r"^event: (.*)$", block, re.MULTILINE)
        data_match = re.search(r"^data: (.*)$", block, re.MULTILINE)
        if event_match and data_match:
            events.append((event_match.group(1), data_match.group(1)))
    return events


def test_query_with_chunks() -> None:
    client = TestClient(app)
    with patch("app.main.retrieve", return_value=FAKE_CHUNKS):
        resp = client.post(
            "/query",
            json={"conversation_id": "11111111-1111-1111-1111-111111111111", "question": "מה מדיניות ההחזרים?"},
        )
    assert resp.status_code == 200, resp.text
    events = _parse_sse(resp.text)
    kinds = [e for e, _ in events]
    assert kinds.count("token") >= 1, events
    assert kinds.count("citation") == len(FAKE_CHUNKS), events
    assert kinds[-1] == "done", events
    print("test_query_with_chunks: OK --", len(events), "events")
    for kind, data in events:
        print(f"  [{kind}] {data}")


def test_query_without_chunks() -> None:
    client = TestClient(app)
    with patch("app.main.retrieve", return_value=[]):
        resp = client.post(
            "/query",
            json={"conversation_id": "22222222-2222-2222-2222-222222222222", "question": "מה המחיר?"},
        )
    assert resp.status_code == 200, resp.text
    events = _parse_sse(resp.text)
    kinds = [e for e, _ in events]
    assert "citation" not in kinds, events
    assert kinds[-1] == "done", events
    token_text = "".join(data for kind, data in events if kind == "token")
    assert "לא נמצא" in token_text, token_text
    print("test_query_without_chunks: OK --", len(events), "events")
    for kind, data in events:
        print(f"  [{kind}] {data}")


def test_schema_proxy_502_when_unreachable() -> None:
    client = TestClient(app)
    resp = client.get("/files/does-not-exist/schema")
    assert resp.status_code == 502, resp.text
    print("test_schema_proxy_502_when_unreachable: OK --", resp.json())


if __name__ == "__main__":
    test_query_with_chunks()
    test_query_without_chunks()
    test_schema_proxy_502_when_unreachable()
    print("\nAll mock-flow smoke tests passed.")
