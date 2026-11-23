"""Generation layer for retrieval-orchestrator.

``generate_stream(prompt, context_chunks=None)`` follows the same MOCK_LLM
mock/real contract as the rest of the system (see shared/CONTRACTS.md
"Mock mode"):

- ``MOCK_LLM=true``: returns a template answer that concatenates the
  retrieved chunks ("תשובה מדומה המבוססת על: ..."), streamed in small
  pieces so the SSE `token` event path is exercised the same way it would
  be in real mode.
- ``MOCK_LLM=false``: calls the OpenAI-compatible
  ``VLLM_GENERATION_URL`` ``/chat/completions`` endpoint with
  ``stream=true`` and yields each delta as it arrives.

``context_chunks`` is accepted in addition to ``prompt`` only so the mock
path can build a readable "based on" summary without re-parsing the
rendered prompt text; real mode ignores it (the prompt already contains
the context). Its three possible shapes mean three different things to the
mock path -- real mode doesn't need the distinction since it just forwards
whatever `prompt` says:
  - ``None``: no retrieval was even attempted -- the "general" route (no
    files at all in the conversation, see app/main.py:_handle_general).
  - ``[]``: retrieval *was* attempted (factual/multi route) but found
    nothing -- the conversation has files, just none relevant/indexed yet.
  - non-empty list: the normal case, chunks to build a "based on" answer
    from.
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# thinking_level ("fast"|"standard"|"deep", see CONTRACTS.md) -> the
# reasoning_effort value sent to the generation model. Documented
# assumption about the field name a reasoning-tuned model behind
# VLLM_GENERATION_URL expects (mirrors OpenAI's o-series convention) --
# confirm against muse30b/gemma3's actual serving config before relying on
# it in production.
_REASONING_EFFORT = {"fast": "low", "standard": "medium", "deep": "high"}


def _reasoning_effort(thinking_level: str) -> str:
    return _REASONING_EFFORT.get(thinking_level, "medium")


async def generate_stream(
    prompt: str,
    context_chunks: list[dict[str, Any]] | None = None,
    thinking_level: str = "standard",
    system_prompt: str | None = None,
    images: list[str] | None = None,
) -> AsyncIterator[str]:
    """Yield text deltas for `prompt`. See module docstring for the
    mock/real contract.

    ``system_prompt`` -- the conversation's custom instruction (see
    CONTRACTS.md "system_prompt"), sent as a real system-role message in
    real mode, on top of whatever `prompt` already says.

    ``images`` -- base64 data URLs attached to a "general" route question
    (see app/main.py:_handle_general; every other route always passes
    `None` here). When set, real mode calls VLLM_VISION_URL instead of
    VLLM_GENERATION_URL with an OpenAI-style multimodal content list --
    there's no guarantee the main generation model is itself multimodal,
    so a dedicated vision deployment is used instead (same reasoning as
    the separate VLLM_EMBED_URL/VLLM_RERANK_URL/VLLM_ROUTER_URL split).
    """
    if settings.MOCK_LLM:
        async for delta in _mock_generate_stream(prompt, context_chunks, thinking_level, system_prompt, images):
            yield delta
    else:
        async for delta in _real_generate_stream(prompt, thinking_level, system_prompt, images):
            yield delta


async def _mock_generate_stream(
    prompt: str,
    context_chunks: list[dict[str, Any]] | None,
    thinking_level: str,
    system_prompt: str | None = None,
    images: list[str] | None = None,
) -> AsyncIterator[str]:
    if context_chunks is None:
        # General-chat route: no retrieval was attempted at all, so there's
        # no "based on" context and no "nothing found" framing either --
        # echoes the actual prompt + thinking_level so mock-mode tests can
        # confirm both flowed through correctly.
        full_text = f"תשובה מדומה לשיחה כללית (רמת חשיבה: {thinking_level}) עבור: {prompt}"
    elif context_chunks:
        snippets = []
        for chunk in context_chunks:
            source_ref = chunk.get("source_ref") or "לא ידוע"
            text = (chunk.get("text") or "").strip()
            snippet = text[:200] + ("…" if len(text) > 200 else "")
            snippets.append(f"[{source_ref}] {snippet}")
        body = " | ".join(snippets)
        full_text = f"תשובה מדומה המבוססת על: {body}"
    else:
        full_text = "תשובה מדומה: לא נמצא מידע רלוונטי בשיחה זו (ייתכן שאין קבצים עדיין)."

    # Reflect the two new optional inputs in the mock text too, so mock-mode
    # tests can confirm they actually flowed through this far -- same idea
    # as the thinking_level echo above.
    if images:
        full_text = f"[כולל {len(images)} תמונות מצורפות] {full_text}"
    if system_prompt:
        full_text = f"[system prompt: {system_prompt}] {full_text}"

    # Stream word-by-word to exercise the same `token` event path as real mode.
    words = full_text.split(" ")
    for i, word in enumerate(words):
        delta = word if i == 0 else f" {word}"
        yield delta


async def _real_generate_stream(
    prompt: str,
    thinking_level: str = "standard",
    system_prompt: str | None = None,
    images: list[str] | None = None,
) -> AsyncIterator[str]:
    use_vision = bool(images)
    base_url = settings.VLLM_VISION_URL if use_vision else settings.VLLM_GENERATION_URL
    model = settings.VLLM_VISION_MODEL if use_vision else settings.VLLM_GENERATION_MODEL
    url = f"{base_url.rstrip('/')}/chat/completions"

    messages: list[dict[str, Any]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    if images:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        content.extend({"type": "image_url", "image_url": {"url": img}} for img in images)
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "reasoning_effort": _reasoning_effort(thinking_level),
    }
    async with httpx.AsyncClient(timeout=settings.HTTP_TIMEOUT_SECONDS) as client:
        async with client.stream("POST", url, json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                if not line.startswith("data:"):
                    continue
                data_str = line[len("data:") :].strip()
                if data_str == "[DONE]":
                    break
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    logger.warning("Could not parse generation stream chunk: %r", data_str)
                    continue
                choices = data.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    yield content
