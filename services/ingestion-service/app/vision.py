"""Vision-model captioning for images embedded in a document, with
MOCK_LLM support per CONTRACTS.md.

Used by app/parsing.py's embedded-image extraction (see
``_caption_embedded_images``) to turn a photo/chart/diagram embedded
alongside real text on a PDF page into an indexable text description --
otherwise that visual content is completely invisible to RAG. This is
unrelated to the existing OCR fallback for *whole-page* scans
(parsing.py:_extract_pdf_pages): that handles a page that IS one big
scanned image with no text layer at all, whereas this handles a normal
text page that additionally contains a smaller embedded picture.
"""
from __future__ import annotations

import base64
import logging

import requests

from app.config import settings

logger = logging.getLogger(__name__)

_CAPTION_INSTRUCTION = (
    "תאר בקצרה ובמדויק מה מופיע בתמונה הזו, כולל כל טקסט גלוי בתוכה אם יש. "
    "התשובה תשמש כטקסט חיפוש במסמך, ולכן התמקד בתוכן עובדתי וקונקרטי."
)


def caption_image(image_bytes: bytes, mime_type: str = "image/png") -> str:
    """Return a short text description of `image_bytes`, or "" on any
    failure. Best-effort by design -- one bad/unreachable-vision-model
    embedded image must never fail the whole document's ingestion (same
    policy as the OCR fallback elsewhere in this service).
    """
    if settings.MOCK_LLM:
        return f"[תיאור מדומה של תמונה מוטבעת, {len(image_bytes)} bytes]"

    url = f"{settings.VLLM_VISION_URL.rstrip('/')}/chat/completions"
    data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    payload = {
        "model": settings.VLLM_VISION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _CAPTION_INSTRUCTION},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        "stream": False,
    }
    try:
        resp = requests.post(url, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        return (data["choices"][0]["message"]["content"] or "").strip()
    except Exception as exc:
        logger.warning("caption_image: call to %s failed: %s", url, exc)
        return ""
