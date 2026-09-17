from __future__ import annotations

import base64
import codecs
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import fitz
import httpx

from app.config import Settings


class OCRServiceError(RuntimeError):
    pass


@dataclass(frozen=True)
class OCRResult:
    raw_text: str
    clean_markdown: str
    page_count: int
    metadata: dict[str, Any] = field(default_factory=dict)
    likelihood_data: dict[str, Any] = field(default_factory=dict)


_DET_BLOCK = re.compile(
    r"<\|det\|>([^<\s]+)(?:\s*\[[^\]]*\])?\s*<\|/det\|>(.*)", re.DOTALL
)
_REF_TOKEN = re.compile(r"<\|/?ref\|>")
_STRUCTURAL_TOKEN = re.compile(r"<\|det\|>.*?<\|/det\|>|<\|/?ref\|>|<\|[^>]+\|>")


def _without_ref_tokens(text: str, raw_start: int) -> tuple[str, list[dict[str, int]]]:
    output: list[str] = []
    spans: list[dict[str, int]] = []
    clean_offset = 0
    cursor = 0
    for marker in _REF_TOKEN.finditer(text):
        if marker.start() > cursor:
            chunk = text[cursor : marker.start()]
            output.append(chunk)
            spans.append(
                {
                    "clean_start": clean_offset,
                    "clean_end": clean_offset + len(chunk),
                    "raw_start": raw_start + cursor,
                    "raw_end": raw_start + marker.start(),
                }
            )
            clean_offset += len(chunk)
        cursor = marker.end()
    if cursor < len(text):
        chunk = text[cursor:]
        output.append(chunk)
        spans.append(
            {
                "clean_start": clean_offset,
                "clean_end": clean_offset + len(chunk),
                "raw_start": raw_start + cursor,
                "raw_end": raw_start + len(text),
            }
        )
    return "".join(output), spans


def clean_ocr_output_with_alignment(raw: str) -> tuple[str, list[dict[str, int]]]:
    """Clean grounding markup and retain clean-to-raw character spans."""
    blocks: list[list[tuple[str, list[dict[str, int]]]]] = []
    current: list[tuple[str, list[dict[str, int]]]] | None = None
    raw_offset = 0
    for raw_line_with_end in raw.splitlines(keepends=True):
        raw_line = raw_line_with_end.rstrip("\r\n")
        line = raw_line.rstrip()
        if not line:
            raw_offset += len(raw_line_with_end)
            continue
        match = _DET_BLOCK.match(line)
        if match:
            category = match.group(1).strip()
            if current is not None:
                blocks.append(current)
            current = []
            content = match.group(2)
            leading = len(content) - len(content.lstrip())
            stripped = content.strip()
            if category != "image" and stripped:
                text, spans = _without_ref_tokens(
                    stripped,
                    raw_offset + match.start(2) + leading,
                )
                if text:
                    current.append((text, spans))
        else:
            if current is None:
                current = []
            text, spans = _without_ref_tokens(line, raw_offset)
            if text:
                current.append((text, spans))
        raw_offset += len(raw_line_with_end)
    if current is not None:
        blocks.append(current)

    clean_parts: list[str] = []
    aligned_spans: list[dict[str, int]] = []
    clean_offset = 0
    nonempty_blocks = [block for block in blocks if block]
    for block_index, block in enumerate(nonempty_blocks):
        if block_index:
            clean_parts.append("\n\n")
            clean_offset += 2
        for line_index, (text, spans) in enumerate(block):
            if line_index:
                clean_parts.append("\n")
                clean_offset += 1
            clean_parts.append(text)
            for span in spans:
                aligned_spans.append(
                    {
                        **span,
                        "clean_start": clean_offset + span["clean_start"],
                        "clean_end": clean_offset + span["clean_end"],
                    }
                )
            clean_offset += len(text)
    untrimmed = "".join(clean_parts)
    left_trim = len(untrimmed) - len(untrimmed.lstrip())
    right_boundary = len(untrimmed.rstrip())
    if not untrimmed or left_trim >= right_boundary:
        return "", []
    trimmed_spans: list[dict[str, int]] = []
    for span in aligned_spans:
        start = max(left_trim, span["clean_start"])
        end = min(right_boundary, span["clean_end"])
        if start >= end:
            continue
        trimmed_spans.append(
            {
                "clean_start": start - left_trim,
                "clean_end": end - left_trim,
                "raw_start": span["raw_start"] + start - span["clean_start"],
                "raw_end": span["raw_start"] + end - span["clean_start"],
            }
        )
    return untrimmed[left_trim:right_boundary], trimmed_spans


def clean_ocr_output(raw: str) -> str:
    """Remove grounding markers while preserving block and reading order."""
    return clean_ocr_output_with_alignment(raw)[0]


def _token_likelihood_data(raw: str, choice: dict[str, Any]) -> dict[str, Any]:
    logprobs = choice.get("logprobs")
    content = logprobs.get("content") if isinstance(logprobs, dict) else None
    if not isinstance(content, list) or not content:
        return {
            "schema_version": 1,
            "available": False,
            "alignment_status": "unavailable",
            "warning": "vLLM did not return generated-token log-probabilities.",
            "tokens": [],
            "clean_to_raw_spans": [],
        }

    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    reconstructed_parts: list[str] = []
    tokens: list[dict[str, Any]] = []
    raw_offset = 0
    malformed = False
    for item in content:
        if not isinstance(item, dict):
            malformed = True
            continue
        byte_values = item.get("bytes")
        if isinstance(byte_values, list) and all(
            isinstance(value, int) and 0 <= value <= 255 for value in byte_values
        ):
            token_bytes = bytes(byte_values)
        elif isinstance(item.get("token"), str):
            token_bytes = item["token"].encode("utf-8", errors="replace")
            byte_values = list(token_bytes)
        else:
            malformed = True
            continue
        decoded = decoder.decode(token_bytes, final=False)
        start = raw_offset
        raw_offset += len(decoded)
        reconstructed_parts.append(decoded)
        logprob = item.get("logprob")
        if not isinstance(logprob, (int, float)):
            malformed = True
            logprob = None
        tokens.append(
            {
                "token": item.get("token", decoded),
                "bytes": byte_values,
                "logprob": float(logprob) if logprob is not None else None,
                "raw_start": start,
                "raw_end": raw_offset,
                "structural": False,
            }
        )
    tail = decoder.decode(b"", final=True)
    if tail:
        reconstructed_parts.append(tail)
        raw_offset += len(tail)
        if tokens:
            tokens[-1]["raw_end"] = raw_offset
    reconstructed = "".join(reconstructed_parts)
    structural_ranges = [match.span() for match in _STRUCTURAL_TOKEN.finditer(raw)]
    for token in tokens:
        token["structural"] = any(
            token["raw_start"] >= start and token["raw_end"] <= end
            for start, end in structural_ranges
        )
    aligned = reconstructed == raw and not malformed
    warning = None
    if malformed:
        warning = "vLLM returned malformed token likelihood entries."
    elif reconstructed != raw:
        warning = "Generated-token bytes did not align with the OCR response text."
    return {
        "schema_version": 1,
        "available": aligned,
        "alignment_status": "aligned" if aligned else "failed",
        "warning": warning,
        "tokens": tokens,
        "clean_to_raw_spans": [],
    }


class UnlimitedOCRClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def extract(self, source: bytes, content_type: str) -> OCRResult:
        with tempfile.TemporaryDirectory(prefix="invoice-ocr-") as directory:
            paths = self._prepare_images(source, content_type, Path(directory))
            raw, metadata, likelihood_data = self._invoke(paths)
        clean, clean_spans = clean_ocr_output_with_alignment(raw)
        likelihood_data["clean_to_raw_spans"] = clean_spans
        if not clean:
            raise OCRServiceError("Unlimited-OCR returned no usable text.")
        return OCRResult(
            raw_text=raw,
            clean_markdown=clean,
            page_count=len(paths),
            metadata=metadata,
            likelihood_data=likelihood_data,
        )

    def clear_temporary_state(self) -> None:
        """Best-effort cleanup of vLLM request caches before a single retry."""
        with httpx.Client(timeout=10) as client:
            for endpoint in ("reset_mm_cache", "reset_prefix_cache"):
                try:
                    client.post(
                        f"{self.settings.ocr_base_url.rstrip('/')}/{endpoint}"
                    ).raise_for_status()
                except httpx.HTTPError:
                    # These administrative endpoints vary by vLLM release and
                    # cache types are disabled in the serving recipe anyway.
                    continue

    def _prepare_images(
        self, source: bytes, content_type: str, directory: Path
    ) -> list[Path]:
        if content_type != "application/pdf":
            extension = ".jpg" if content_type == "image/jpeg" else ".png"
            path = directory / f"page_0001{extension}"
            path.write_bytes(source)
            return [path]

        document = fitz.open(stream=source, filetype="pdf")
        paths: list[Path] = []
        try:
            matrix = fitz.Matrix(
                self.settings.pdf_render_dpi / 72,
                self.settings.pdf_render_dpi / 72,
            )
            for index, page in enumerate(document):
                path = directory / f"page_{index + 1:04d}.png"
                page.get_pixmap(matrix=matrix, alpha=False).save(path)
                paths.append(path)
        finally:
            document.close()
        return paths

    def _invoke(
        self, image_paths: list[Path]
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        multi_page = len(image_paths) > 1
        max_tokens = (
            self.settings.ocr_max_tokens
            if multi_page
            else self.settings.ocr_single_page_max_tokens
        )
        prompt = "<image>Multi page parsing." if multi_page else "<image>document parsing."
        content: list[dict[str, object]] = [{"type": "text", "text": prompt}]
        for image_path in image_paths:
            mime = "image/jpeg" if image_path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
            encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{encoded}"},
                }
            )

        payload = {
            "model": self.settings.ocr_model_id,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens,
            "temperature": 0.0,
            "skip_special_tokens": False,
            "vllm_xargs": {
                "ngram_size": 35,
                "window_size": 1024 if multi_page else 128,
            },
        }
        if self.settings.ocr_logprobs_enabled:
            payload.update({"logprobs": True, "top_logprobs": 0})
        try:
            with httpx.Client(timeout=self.settings.ocr_timeout_seconds) as client:
                response = client.post(
                    f"{self.settings.ocr_base_url.rstrip('/')}/v1/chat/completions",
                    json=payload,
                )
                response.raise_for_status()
                body = response.json()
            choice = body["choices"][0]
            raw_text = choice["message"]["content"]
            likelihood_data = _token_likelihood_data(raw_text, choice)
            metadata = {
                "finish_reason": choice.get("finish_reason"),
                "usage": body.get("usage", {}),
                "max_tokens_requested": max_tokens,
                "page_mode": "multi_page" if multi_page else "single_page",
                "page_count": len(image_paths),
                "logprobs": {
                    "requested": self.settings.ocr_logprobs_enabled,
                    "available": likelihood_data["available"],
                    "alignment_status": likelihood_data["alignment_status"],
                    "warning": likelihood_data["warning"],
                    "token_count": len(likelihood_data["tokens"]),
                },
            }
            return raw_text, metadata, likelihood_data
        except httpx.HTTPStatusError as error:
            response = error.response
            try:
                detail = response.json()["error"]["message"]
            except (KeyError, TypeError, ValueError):
                detail = response.text.strip() or str(error)
            raise OCRServiceError(
                f"Unlimited-OCR returned HTTP {response.status_code}: {detail[:2000]}"
            ) from error
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as error:
            raise OCRServiceError(f"Unlimited-OCR request failed: {error}") from error
