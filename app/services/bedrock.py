from __future__ import annotations

import json
import random
import time
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.config import Settings
from app.schemas import InvoiceExtraction


class BedrockServiceError(RuntimeError):
    def __init__(self, message: str, *, metadata: dict[str, Any] | None = None):
        super().__init__(message)
        self.metadata = metadata or {}


@dataclass(frozen=True)
class BedrockResult:
    invoice: InvoiceExtraction
    metadata: dict[str, Any] = field(default_factory=dict)


RETRYABLE_CODES = {"InternalServerException", "ModelNotReadyException", "ModelTimeoutException", "ServiceUnavailableException", "ThrottlingException", "TooManyRequestsException"}

SYSTEM_PROMPT = """You convert OCR Markdown from invoices into faithful, structured accounts-payable data.
Translate non-English text into English. Use only the supplied OCR content. Never infer a value that is not present.
For missing or uncertain scalar fields, return an empty string and add a concise warning. The application converts those
empty strings to null. Preserve monetary strings exactly as shown.
Evidence entries must be short exact snippets copied from the OCR text. Set document_type to Invoice only when the content
is an invoice; otherwise set it to Unknown and explain why in warnings. For every populated scoreable scalar field, add a
field_evidence entry whose field_path is its JSON path (for example invoice.invoice_number or line_items[0].line_total) and
whose snippets are short exact OCR excerpts supporting that specific value. Do not produce confidence scores."""


def _structured_output_schema() -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a compact Bedrock schema and the canonical validation schema.

    A nullable string for every optional invoice field makes Bedrock's compiled
    grammar too complex. The transport schema uses an empty string for missing
    scalar values; the canonical Pydantic schema is retained for normalization
    and final validation.
    """
    canonical = InvoiceExtraction.model_json_schema()
    transport = deepcopy(canonical)

    def compact(node: Any) -> Any:
        if isinstance(node, list):
            return [compact(value) for value in node]
        if not isinstance(node, dict):
            return node

        variants = node.get("anyOf")
        if isinstance(variants, list) and {
            variant.get("type") for variant in variants if isinstance(variant, dict)
        } == {"string", "null"}:
            return {"type": "string"}

        return {
            key: compact(value)
            for key, value in node.items()
            if key not in {"title", "description", "default"}
        }

    return compact(transport), canonical


def _normalize_nullable_strings(
    value: Any, schema: dict[str, Any], root_schema: dict[str, Any]
) -> Any:
    reference = schema.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/$defs/"):
        schema = root_schema["$defs"][reference.rsplit("/", 1)[-1]]

    variants = schema.get("anyOf")
    if isinstance(variants, list) and {
        variant.get("type") for variant in variants if isinstance(variant, dict)
    } == {"string", "null"}:
        return None if value == "" else value

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        return {
            key: _normalize_nullable_strings(item, properties.get(key, {}), root_schema)
            for key, item in value.items()
        }
    if isinstance(value, list):
        item_schema = schema.get("items", {})
        return [
            _normalize_nullable_strings(item, item_schema, root_schema)
            for item in value
        ]
    return value


def _response_metadata(response: dict[str, Any]) -> dict[str, Any]:
    output = response.get("output")
    message = output.get("message") if isinstance(output, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    blocks = content if isinstance(content, list) else []
    return {
        "stop_reason": response.get("stopReason"),
        "usage": response.get("usage", {}),
        "metrics": response.get("metrics", {}),
        "response_shape": {
            "top_level_keys": sorted(response),
            "output_keys": sorted(output) if isinstance(output, dict) else [],
            "message_keys": sorted(message) if isinstance(message, dict) else [],
            "content_block_count": len(blocks),
            "content_block_keys": [
                sorted(block) if isinstance(block, dict) else [type(block).__name__]
                for block in blocks
            ],
        },
    }


class BedrockInvoiceClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = boto3.client("bedrock-runtime", region_name=settings.aws_region)

    def extract(self, ocr_markdown: str) -> BedrockResult:
        schema, canonical_schema = _structured_output_schema()
        request: dict[str, Any] = {
            "modelId": self.settings.bedrock_model_id,
            "system": [{"text": SYSTEM_PROMPT}],
            "messages": [{"role": "user", "content": [{"text": "Translate and extract this OCR Markdown into the required invoice schema:\n\n" + ocr_markdown}]}],
            "inferenceConfig": {"maxTokens": self.settings.bedrock_max_tokens, "temperature": 0},
            "outputConfig": {"textFormat": {"type": "json_schema", "structure": {"jsonSchema": {"schema": json.dumps(schema), "name": "invoice_extraction", "description": "Faithful translated invoice extraction"}}}},
        }
        last_error: Exception | None = None
        last_metadata: dict[str, Any] = {}
        for attempt in range(1, self.settings.bedrock_retry_attempts + 1):
            try:
                response = self.client.converse(**request)
                last_metadata = _response_metadata(response)
                output = response.get("output", {})
                message = output.get("message", {}) if isinstance(output, dict) else {}
                content = message.get("content", []) if isinstance(message, dict) else []
                text_blocks = [
                    block["text"]
                    for block in content
                    if isinstance(block, dict) and isinstance(block.get("text"), str)
                ]
                if not text_blocks:
                    stop_reason = last_metadata.get("stop_reason") or "not provided"
                    raise BedrockServiceError(
                        "Bedrock returned no text content "
                        f"(stopReason={stop_reason}, contentBlocks={len(content)}).",
                        metadata=last_metadata,
                    )
                text = text_blocks[0]
                data = json.loads(text)
                normalized = _normalize_nullable_strings(
                    data, canonical_schema, canonical_schema
                )
                return BedrockResult(
                    invoice=InvoiceExtraction.model_validate(normalized),
                    metadata=last_metadata,
                )
            except BedrockServiceError:
                raise
            except ClientError as error:
                last_error = error
                code = error.response.get("Error", {}).get("Code", "")
                if code not in RETRYABLE_CODES or attempt >= self.settings.bedrock_retry_attempts:
                    break
            except BotoCoreError as error:
                last_error = error
                if attempt >= self.settings.bedrock_retry_attempts:
                    break
            except (KeyError, IndexError, TypeError, ValueError) as error:
                raise BedrockServiceError(
                    f"Bedrock returned an invalid structured response: {error}",
                    metadata=last_metadata,
                ) from error
            time.sleep(min(8.0, 2 ** (attempt - 1)) + random.uniform(0, 0.25))
        raise BedrockServiceError(
            f"Bedrock extraction failed after {self.settings.bedrock_retry_attempts} attempts: {last_error}",
            metadata=last_metadata,
        ) from last_error
