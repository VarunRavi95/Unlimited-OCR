from __future__ import annotations

import fitz
import pytest

from app.services.validation import DocumentValidationError, validate_document


def make_pdf(pages: int) -> bytes:
    document = fitz.open()
    for _ in range(pages):
        page = document.new_page()
        page.insert_text((72, 72), "Invoice")
    data = document.tobytes()
    document.close()
    return data


def test_valid_png_uses_signature_not_extension(png_bytes: bytes) -> None:
    result = validate_document(png_bytes, "wrong.pdf", max_bytes=1_000_000, max_pages=20, minimum_dpi=150)
    assert result.content_type == "image/png"
    assert result.extension == ".png"
    assert result.page_count == 1


def test_pdf_page_limit() -> None:
    with pytest.raises(DocumentValidationError, match="limit is 2"):
        validate_document(make_pdf(3), "invoice.pdf", max_bytes=1_000_000, max_pages=2, minimum_dpi=150)


def test_rejects_encrypted_pdf() -> None:
    document = fitz.open()
    document.new_page()
    data = document.tobytes(
        encryption=fitz.PDF_ENCRYPT_AES_256,
        owner_pw="owner",
        user_pw="user",
    )
    document.close()
    with pytest.raises(DocumentValidationError, match="Encrypted PDFs"):
        validate_document(
            data,
            "invoice.pdf",
            max_bytes=1_000_000,
            max_pages=20,
            minimum_dpi=150,
        )


@pytest.mark.parametrize("data", [b"", b"plain text", b"\x89PNGbad"])
def test_rejects_empty_or_invalid_files(data: bytes) -> None:
    with pytest.raises(DocumentValidationError):
        validate_document(data, "invoice.bin", max_bytes=1_000_000, max_pages=20, minimum_dpi=150)


def test_rejects_oversized_file(png_bytes: bytes) -> None:
    with pytest.raises(DocumentValidationError, match="upload limit"):
        validate_document(png_bytes, "invoice.png", max_bytes=10, max_pages=20, minimum_dpi=150)
