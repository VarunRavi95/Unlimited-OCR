from __future__ import annotations

import json
from decimal import Decimal

from app.config import Settings
from app.schemas import InvoiceExtraction
from app.services.trust import TrustAssessor, _deterministic_signals
from app.trust_models import DocumentTrustStatus, FieldTrustStatus


def complete_invoice() -> tuple[InvoiceExtraction, str]:
    markdown = """Supplier: Example Supplier
Invoice: INV-100
Invoice date: 2026-08-01
Currency: INR
Subtotal: 100.00
Tax: GST | Base 100.00 | Rate 18% | Amount 18.00
Tax total: 18.00
Grand total: 118.00
Amount due: 118.00
Service | 1 | 100.00 | 18.00 | 118.00"""
    payload = {
        "document_type": "Invoice",
        "source_languages": ["English"],
        "translated_markdown": markdown,
        "supplier": {"name": "Example Supplier", "address": None, "tax_id": None, "evidence": ["Supplier: Example Supplier"]},
        "buyer": {"name": None, "address": None, "tax_id": None, "evidence": []},
        "invoice": {"invoice_number": "INV-100", "purchase_order_number": None, "invoice_date": "2026-08-01", "due_date": None, "currency": "INR", "payment_terms": None, "evidence": ["Invoice: INV-100"]},
        "totals": {"subtotal": "100.00", "discount": None, "shipping": None, "tax_total": "18.00", "grand_total": "118.00", "amount_due": "118.00", "taxes": [{"tax_type": "GST", "base_amount": "100.00", "rate": "18%", "amount": "18.00", "evidence": ["Tax: GST | Base 100.00 | Rate 18% | Amount 18.00"]}], "evidence": ["Grand total: 118.00"]},
        "line_items": [{"description_original": "Service", "description_english": "Service", "quantity": "1", "unit": None, "unit_price": "100.00", "tax_amount": "18.00", "line_total": "118.00", "evidence": ["Service | 1 | 100.00 | 18.00 | 118.00"]}],
        "payment": {"bank_name": None, "account_name": None, "account_number": None, "iban": None, "swift_bic": None, "evidence": []},
        "warnings": [],
        "field_evidence": [],
    }
    snippets = {
        "supplier.name": "Supplier: Example Supplier",
        "invoice.invoice_number": "Invoice: INV-100",
        "invoice.invoice_date": "Invoice date: 2026-08-01",
        "invoice.currency": "Currency: INR",
        "totals.subtotal": "Subtotal: 100.00",
        "totals.tax_total": "Tax total: 18.00",
        "totals.grand_total": "Grand total: 118.00",
        "totals.amount_due": "Amount due: 118.00",
        "line_items[0].line_total": "Service | 1 | 100.00 | 18.00 | 118.00",
        "line_items[0].description_original": "Service | 1 | 100.00 | 18.00 | 118.00",
        "line_items[0].quantity": "Service | 1 | 100.00 | 18.00 | 118.00",
        "line_items[0].unit_price": "Service | 1 | 100.00 | 18.00 | 118.00",
        "line_items[0].tax_amount": "Service | 1 | 100.00 | 18.00 | 118.00",
        "totals.taxes[0].tax_type": "Tax: GST | Base 100.00 | Rate 18% | Amount 18.00",
        "totals.taxes[0].base_amount": "Tax: GST | Base 100.00 | Rate 18% | Amount 18.00",
        "totals.taxes[0].rate": "Tax: GST | Base 100.00 | Rate 18% | Amount 18.00",
        "totals.taxes[0].amount": "Tax: GST | Base 100.00 | Rate 18% | Amount 18.00",
    }
    payload["field_evidence"] = [
        {"field_path": path, "snippets": [snippet]} for path, snippet in snippets.items()
    ]
    return InvoiceExtraction.model_validate(payload), markdown


def identity_likelihoods(markdown: str) -> dict[str, object]:
    tokens = []
    cursor = 0
    for token in markdown.splitlines(keepends=True):
        tokens.append(
            {
                "token": token,
                "bytes": list(token.encode()),
                "logprob": -0.1,
                "raw_start": cursor,
                "raw_end": cursor + len(token),
                "structural": False,
            }
        )
        cursor += len(token)
    return {
        "available": True,
        "alignment_status": "aligned",
        "warning": None,
        "tokens": tokens,
        "clean_to_raw_spans": [{"clean_start": 0, "clean_end": len(markdown), "raw_start": 0, "raw_end": len(markdown)}],
    }


def test_signals_mode_uses_evidence_not_raw_likelihood(tmp_path) -> None:
    invoice, markdown = complete_invoice()
    assessor = TrustAssessor(Settings(_env_file=None, data_dir=tmp_path, trust_mode="signals"))
    assessment = assessor.assess(
        invoice,
        clean_markdown=markdown,
        likelihood_data=identity_likelihoods(markdown),
        validation_warnings=[],
        ocr_metadata={"finish_reason": "stop"},
        page_count=1,
    )

    assert assessment.document.status == DocumentTrustStatus.READY
    invoice_number = next(field for field in assessment.fields if field.field_path == "invoice.invoice_number")
    assert invoice_number.status == FieldTrustStatus.SUPPORTED
    assert invoice_number.calibrated_confidence is None
    assert invoice_number.diagnostic_likelihood is not None


def test_missing_evidence_and_bad_totals_force_review(tmp_path) -> None:
    invoice, markdown = complete_invoice()
    invoice.field_evidence = [
        reference for reference in invoice.field_evidence if reference.field_path != "invoice.invoice_number"
    ]
    invoice.totals.grand_total = "999.00"
    assessor = TrustAssessor(Settings(_env_file=None, data_dir=tmp_path))
    assessment = assessor.assess(
        invoice,
        clean_markdown=markdown,
        likelihood_data={},
        validation_warnings=[],
        ocr_metadata={"finish_reason": "stop"},
        page_count=1,
    )

    invoice_number = next(field for field in assessment.fields if field.field_path == "invoice.invoice_number")
    grand_total = next(field for field in assessment.fields if field.field_path == "totals.grand_total")
    assert invoice_number.status == FieldTrustStatus.UNSUPPORTED
    assert grand_total.status == FieldTrustStatus.UNSUPPORTED
    assert assessment.document.status == DocumentTrustStatus.INSUFFICIENT_SUPPORT


def test_calibrated_percentage_requires_promoted_artifact(tmp_path) -> None:
    invoice, markdown = complete_invoice()
    calibration = tmp_path / "calibration.json"
    calibration.write_text(
        json.dumps(
            {
                "version": "test-v1",
                "promoted": True,
                "feature_names": [],
                "means": {},
                "scales": {},
                "coefficients": {},
                "intercept": 4.0,
                "isotonic": {"x": [1.0], "y": [0.97]},
            }
        ),
        encoding="utf-8",
    )
    assessor = TrustAssessor(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            trust_mode="calibrated",
            trust_calibration_path=calibration,
        )
    )
    assessment = assessor.assess(
        invoice,
        clean_markdown=markdown,
        likelihood_data=identity_likelihoods(markdown),
        validation_warnings=[],
        ocr_metadata={"finish_reason": "stop"},
        page_count=1,
    )
    assert assessment.calibration_version == "test-v1"
    assert all(
        field.calibrated_confidence == 0.97
        for field in assessment.fields
        if field.value not in {None, ""}
    )


def test_tax_and_currency_contradictions_are_hard_validation_failures() -> None:
    invoice, _ = complete_invoice()
    invoice.totals.taxes[0].amount = "19.00"
    invoice.totals.grand_total = "USD 118.00"

    signals, reasons = _deterministic_signals(invoice, Decimal("0.02"))

    tax_codes = {signal.code for signal in signals["totals.taxes[0].amount"] if signal.passed is False}
    currency_codes = {signal.code for signal in signals["invoice.currency"] if signal.passed is False}
    assert "tax_arithmetic" in tax_codes
    assert "tax_total_reconciliation" in tax_codes
    assert "currency_consistency" in currency_codes
    assert any("Tax entry" in reason for reason in reasons)
    assert any("currency marker" in reason for reason in reasons)
