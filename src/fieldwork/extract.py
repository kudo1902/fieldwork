"""Orchestration: bytes in, validated structured data out.

Synchronous by design. Everything expensive here is either I/O (waiting on
vLLM) or CPU work in Pillow that releases the GIL, so threads handle
concurrency perfectly well and the code stays readable.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from . import preprocess, schemas
from .llm import ExtractionResult, extract as _extract


def extract_bytes(
    data: bytes,
    schema_name: str,
    *,
    max_dim: int | None = None,
) -> ExtractionResult:
    doctype = schemas.get(schema_name)
    digest = hashlib.sha256(data).hexdigest()

    pages = preprocess.to_page_images(data, max_dim=max_dim)
    urls = preprocess.to_data_urls(pages)

    result = _extract(urls, doctype)
    result.source_sha256 = digest
    return result


def extract_file(
    path: str | Path,
    schema_name: str,
    *,
    max_dim: int | None = None,
) -> ExtractionResult:
    return extract_bytes(Path(path).read_bytes(), schema_name, max_dim=max_dim)
