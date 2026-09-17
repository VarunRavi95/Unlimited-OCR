from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class FieldTrustStatus(StrEnum):
    SUPPORTED = "supported"
    REVIEW = "review"
    UNSUPPORTED = "unsupported"
    NOT_ASSESSED = "not_assessed"


class DocumentTrustStatus(StrEnum):
    READY = "ready"
    REVIEW_REQUIRED = "review_required"
    INSUFFICIENT_SUPPORT = "insufficient_support"


class EvidenceMatchType(StrEnum):
    EXACT = "exact"
    NORMALIZED = "normalized"
    NONE = "none"


class SignalSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class EvidenceReference(BaseModel):
    """Bedrock-supplied exact OCR snippets for one extracted field."""

    model_config = ConfigDict(extra="forbid")

    field_path: str
    snippets: list[str]


class TrustSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    passed: bool | None
    severity: SignalSeverity
    message: str
    observed: Any | None = None
    expected: Any | None = None


class EvidenceMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snippet: str
    match_type: EvidenceMatchType
    value_supported: bool
    occurrences: int | None = None
    clean_start: int | None = None
    clean_end: int | None = None
    alignment_status: str = "unavailable"
    geometric_mean_likelihood: float | None = None
    p10_logprob: float | None = None
    minimum_logprob: float | None = None
    token_count: int = 0


class FieldTrust(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_path: str
    value: Any | None
    status: FieldTrustStatus
    critical: bool = False
    calibrated_confidence: float | None = None
    diagnostic_likelihood: float | None = None
    evidence_matches: list[EvidenceMatch] = Field(default_factory=list)
    validation_signals: list[TrustSignal] = Field(default_factory=list)
    review_reasons: list[str] = Field(default_factory=list)


class DocumentTrust(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: DocumentTrustStatus
    review_reasons: list[str] = Field(default_factory=list)
    critical_fields: list[str] = Field(default_factory=list)


class TrustAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assessment_version: str
    ruleset_version: str
    calibration_version: str | None = None
    mode: str
    document: DocumentTrust
    fields: list[FieldTrust]
    disclaimer: str
    warnings: list[str] = Field(default_factory=list)
