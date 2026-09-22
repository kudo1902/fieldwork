"""The one place that talks to the model.

Everything goes through an OpenAI-compatible endpoint, so swapping vLLM for
Ollama, LM Studio, or a hosted API is a base-URL change and nothing else.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI
from pydantic import ValidationError

from .config import settings
from .prompts import REPAIR_INSTRUCTION, SYSTEM_PROMPT, USER_INSTRUCTION
from .schemas import DocumentType, json_schema_for

_client: OpenAI | None = None


def client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            timeout=settings.request_timeout,
            max_retries=1,
        )
    return _client


@dataclass
class ExtractionResult:
    ok: bool
    schema: str
    data: dict[str, Any] | None = None
    error: str | None = None
    raw: str = ""
    attempts: int = 0
    latency_s: float = 0.0
    pages: int = 0
    usage: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    source_sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "schema": self.schema,
            "data": self.data,
            "error": self.error,
            "attempts": self.attempts,
            "latency_s": round(self.latency_s, 3),
            "pages": self.pages,
            "usage": self.usage,
            "model": self.model,
            "source_sha256": self.source_sha256,
        }


def _user_content(image_urls: list[str], hint: str) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [
        {"type": "image_url", "image_url": {"url": url}} for url in image_urls
    ]
    content.append({"type": "text", "text": USER_INSTRUCTION.format(hint=hint)})
    return content


def extract(image_urls: list[str], doctype: DocumentType) -> ExtractionResult:
    schema = json_schema_for(doctype.model)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _user_content(image_urls, doctype.hint)},
    ]

    started = time.perf_counter()
    last_error = "no attempt made"
    raw = ""
    usage: dict[str, Any] = {}

    for attempt in range(1, settings.max_repair_attempts + 2):
        try:
            resp = client().chat.completions.create(
                model=settings.llm_model,
                messages=messages,
                temperature=settings.temperature,
                max_tokens=settings.max_tokens,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": doctype.name,
                        "schema": schema,
                        "strict": True,
                    },
                },
                extra_body=settings.extra_body or None,
            )
        except Exception as exc:  # transport / server error -- not repairable
            return ExtractionResult(
                ok=False,
                schema=doctype.name,
                error=f"{type(exc).__name__}: {exc}",
                attempts=attempt,
                latency_s=time.perf_counter() - started,
                pages=len(image_urls),
                model=settings.llm_model,
            )

        raw = resp.choices[0].message.content or ""
        if resp.usage:
            usage = resp.usage.model_dump()

        try:
            parsed = doctype.model.model_validate_json(raw)
            return ExtractionResult(
                ok=True,
                schema=doctype.name,
                data=parsed.model_dump(mode="json"),
                raw=raw,
                attempts=attempt,
                latency_s=time.perf_counter() - started,
                pages=len(image_urls),
                usage=usage,
                model=settings.llm_model,
            )
        except (ValidationError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            messages.append({"role": "assistant", "content": raw})
            messages.append(
                {"role": "user", "content": REPAIR_INSTRUCTION.format(error=last_error)}
            )

    return ExtractionResult(
        ok=False,
        schema=doctype.name,
        error=f"schema validation failed after repairs: {last_error}",
        raw=raw,
        attempts=settings.max_repair_attempts + 1,
        latency_s=time.perf_counter() - started,
        pages=len(image_urls),
        usage=usage,
        model=settings.llm_model,
    )
