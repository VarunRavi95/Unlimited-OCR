from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "Unlimited-OCR Invoice PoC"
    app_version: str = "0.1.0"
    aws_region: str = "ap-south-1"
    s3_bucket: str = ""
    storage_backend: str = "s3"
    data_dir: Path = Path("data")

    ocr_base_url: str = "http://ocr:8000"
    ocr_model_id: str = "baidu/Unlimited-OCR"
    ocr_model_revision: str = "d549bb9d6a055dbe291408916d66acc2cd5920f6"
    ocr_timeout_seconds: int = 1800
    # Single-page invoices use a smaller ceiling for acceptable T4 latency.
    # Multi-page documents retain the official recipe's larger output budget.
    ocr_single_page_max_tokens: int = 4096
    ocr_max_tokens: int = 8192
    ocr_logprobs_enabled: bool = True

    bedrock_model_id: str = "global.anthropic.claude-sonnet-4-6"
    bedrock_max_tokens: int = 16384
    bedrock_retry_attempts: int = 3

    max_upload_bytes: int = 20 * 1024 * 1024
    max_pages: int = 20
    pdf_render_dpi: int = 300
    minimum_benchmark_dpi: int = 150
    trust_mode: Literal["signals", "calibrated"] = "signals"
    trust_calibration_path: Path | None = None
    trust_supported_threshold: float = Field(default=0.90, ge=0, le=1)
    trust_review_threshold: float = Field(default=0.70, ge=0, le=1)
    trust_amount_tolerance: float = Field(default=0.02, ge=0)
    trust_ruleset_version: str = "1.0.0"
    worker_enabled: bool = True
    cloudwatch_log_group: str = "/unlimited-ocr-poc/app"

    allowed_content_types: tuple[str, ...] = Field(
        default=("application/pdf", "image/jpeg", "image/png")
    )

    @model_validator(mode="after")
    def validate_trust_thresholds(self) -> Settings:
        if self.trust_supported_threshold <= self.trust_review_threshold:
            raise ValueError(
                "TRUST_SUPPORTED_THRESHOLD must be greater than "
                "TRUST_REVIEW_THRESHOLD"
            )
        return self

    @property
    def database_path(self) -> Path:
        return self.data_dir / "jobs.db"

    @property
    def local_storage_path(self) -> Path:
        return self.data_dir / "storage"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings
