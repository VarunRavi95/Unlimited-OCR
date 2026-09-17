from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.trust_models import EvidenceReference, TrustAssessment


class JobStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class JobStage(StrEnum):
    QUEUED = "queued"
    VALIDATING = "validating"
    OCR = "ocr"
    BEDROCK = "bedrock"
    PERSISTING = "persisting"
    COMPLETED = "completed"
    FAILED = "failed"


class Party(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None
    address: str | None
    tax_id: str | None
    evidence: list[str]


class InvoiceDetails(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice_number: str | None
    purchase_order_number: str | None
    invoice_date: str | None
    due_date: str | None
    currency: str | None
    payment_terms: str | None
    evidence: list[str]


class TaxEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tax_type: str | None
    base_amount: str | None
    rate: str | None
    amount: str | None
    evidence: list[str]


class Totals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subtotal: str | None
    discount: str | None
    shipping: str | None
    tax_total: str | None
    grand_total: str | None
    amount_due: str | None
    taxes: list[TaxEntry]
    evidence: list[str]


class LineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description_original: str | None
    description_english: str | None
    quantity: str | None
    unit: str | None
    unit_price: str | None
    tax_amount: str | None
    line_total: str | None
    evidence: list[str]


class PaymentDetails(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bank_name: str | None
    account_name: str | None
    account_number: str | None
    iban: str | None
    swift_bic: str | None
    evidence: list[str]


class InvoiceExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_type: str
    source_languages: list[str]
    translated_markdown: str
    supplier: Party
    buyer: Party
    invoice: InvoiceDetails
    totals: Totals
    line_items: list[LineItem]
    payment: PaymentDetails
    warnings: list[str]
    field_evidence: list[EvidenceReference]


class JobRecord(BaseModel):
    id: str
    status: JobStatus
    stage: JobStage
    filename: str
    content_type: str
    source_key: str
    page_count: int
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None
    retry_count: int = 0
    validation_warnings: list[str] = Field(default_factory=list)
    timings: dict[str, float] = Field(default_factory=dict)
    artifacts: dict[str, str] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    trust: TrustAssessment | None = None


class JobCreated(BaseModel):
    job_id: str
    status: JobStatus
    created_at: datetime
