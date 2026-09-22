from __future__ import annotations

import json
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FIELDWORK_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm_base_url: str = "http://localhost:8000/v1"
    llm_api_key: str = "EMPTY"
    llm_model: str = "gemma-4-12B-it"

    max_image_dim: int = 1536
    jpeg_quality: int = 90
    pdf_render_scale: float = 2.0

    extra_body: dict[str, Any] = Field(default_factory=dict)

    temperature: float = 0.0
    max_tokens: int = 4096
    request_timeout: float = 180.0
    max_repair_attempts: int = 2

    max_upload_mb: int = 20
    max_pdf_pages: int = 8

    @field_validator("extra_body", mode="before")
    @classmethod
    def _parse_extra_body(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()
            return json.loads(v) if v else {}
        return v


settings = Settings()
