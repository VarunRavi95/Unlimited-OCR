from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

from app.schemas import InvoiceExtraction


@pytest.fixture
def png_bytes() -> bytes:
    stream = BytesIO()
    Image.new("RGB", (320, 180), "white").save(stream, format="PNG", dpi=(300, 300))
    return stream.getvalue()


@pytest.fixture
def extraction() -> InvoiceExtraction:
    return InvoiceExtraction.model_validate({
        "document_type": "Invoice",
        "source_languages": ["English"],
        "translated_markdown": "# Invoice INV-100\nTotal INR 118.00",
        "supplier": {"name": "Example Supplier", "address": None, "tax_id": None, "evidence": ["Example Supplier"]},
        "buyer": {"name": None, "address": None, "tax_id": None, "evidence": []},
        "invoice": {"invoice_number": "INV-100", "purchase_order_number": None, "invoice_date": "2026-08-01", "due_date": None, "currency": "INR", "payment_terms": None, "evidence": ["INV-100"]},
        "totals": {"subtotal": "100.00", "discount": None, "shipping": None, "tax_total": "18.00", "grand_total": "118.00", "amount_due": "118.00", "taxes": [{"tax_type": "GST", "base_amount": None, "rate": "18%", "amount": "18.00", "evidence": ["GST 18.00"]}], "evidence": ["Total INR 118.00"]},
        "line_items": [{"description_original": "Service", "description_english": "Service", "quantity": "1", "unit": None, "unit_price": "100.00", "tax_amount": "18.00", "line_total": "118.00", "evidence": ["Service 1 100.00"]}],
        "payment": {"bank_name": None, "account_name": None, "account_number": None, "iban": None, "swift_bic": None, "evidence": []},
        "warnings": [],
        "field_evidence": [
            {"field_path": "supplier.name", "snippets": ["Example Supplier"]},
            {"field_path": "invoice.invoice_number", "snippets": ["INV-100"]},
            {"field_path": "invoice.invoice_date", "snippets": ["2026-08-01"]},
            {"field_path": "invoice.currency", "snippets": ["INR"]},
            {"field_path": "totals.grand_total", "snippets": ["Total INR 118.00"]},
        ],
    })
