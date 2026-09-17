from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import fitz
from PIL import Image, UnidentifiedImageError


class DocumentValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedDocument:
    content_type: str
    extension: str
    page_count: int
    warnings: tuple[str, ...] = ()


def validate_document(data: bytes, filename: str, *, max_bytes: int, max_pages: int, minimum_dpi: int) -> ValidatedDocument:
    if not data:
        raise DocumentValidationError("The uploaded file is empty.")
    if len(data) > max_bytes:
        raise DocumentValidationError(f"File exceeds the {max_bytes // (1024 * 1024)} MB upload limit.")
    suffix = Path(filename or "").suffix.lower()
    if data.startswith(b"%PDF-"):
        return _validate_pdf(data, max_pages)
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return _validate_image(data, "image/png", ".png", minimum_dpi)
    if data.startswith(b"\xff\xd8\xff"):
        return _validate_image(data, "image/jpeg", ".jpg", minimum_dpi)
    raise DocumentValidationError(f"Unsupported file signature{f' for {suffix}' if suffix else ''}; expected PDF, JPEG, or PNG.")


def _validate_pdf(data: bytes, max_pages: int) -> ValidatedDocument:
    try:
        document = fitz.open(stream=data, filetype="pdf")
    except Exception as error:
        raise DocumentValidationError("The PDF is corrupt or unreadable.") from error
    try:
        if document.needs_pass:
            raise DocumentValidationError("Encrypted PDFs are not supported in this PoC.")
        page_count = document.page_count
        if page_count < 1:
            raise DocumentValidationError("The PDF contains no pages.")
        if page_count > max_pages:
            raise DocumentValidationError(f"PDF contains {page_count} pages; the limit is {max_pages}.")
    finally:
        document.close()
    return ValidatedDocument("application/pdf", ".pdf", page_count)


def _validate_image(data: bytes, content_type: str, extension: str, minimum_dpi: int) -> ValidatedDocument:
    try:
        with Image.open(BytesIO(data)) as image:
            image.verify()
        with Image.open(BytesIO(data)) as image:
            dpi = image.info.get("dpi")
    except (UnidentifiedImageError, OSError) as error:
        raise DocumentValidationError("The image is corrupt or unreadable.") from error
    warnings: list[str] = []
    if dpi:
        measured = min(float(dpi[0]), float(dpi[1]))
        if measured < minimum_dpi:
            warnings.append(f"Image metadata reports {measured:.0f} DPI, below the {minimum_dpi} DPI benchmark prerequisite.")
    else:
        warnings.append("Image DPI metadata is unavailable; extraction quality may vary.")
    return ValidatedDocument(content_type, extension, 1, tuple(warnings))
