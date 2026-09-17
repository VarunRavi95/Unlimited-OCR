from __future__ import annotations

import json

import httpx
import pytest
from botocore.exceptions import ClientError

from app.config import Settings
from app.schemas import InvoiceExtraction
from app.services.bedrock import (
    BedrockInvoiceClient,
    BedrockServiceError,
    _normalize_nullable_strings,
    _structured_output_schema,
)
from app.services.ocr import OCRServiceError, UnlimitedOCRClient


class FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        raw = "raw OCR"
        return {
            "choices": [
                {
                    "message": {"content": raw},
                    "finish_reason": "stop",
                    "logprobs": {
                        "content": [
                            {
                                "token": raw,
                                "bytes": list(raw.encode("utf-8")),
                                "logprob": -0.1,
                                "top_logprobs": [],
                            }
                        ]
                    },
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 25},
        }


class CapturingHttpClient:
    def __init__(self, captured: list[dict[str, object]], **kwargs: object):
        del kwargs
        self.captured = captured

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def post(self, url: str, json: dict[str, object]) -> FakeResponse:
        self.captured.append({"url": url, "payload": json})
        return FakeResponse()


def test_ocr_client_preserves_official_single_and_multi_page_recipe(
    tmp_path, monkeypatch
) -> None:
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(
        "app.services.ocr.httpx.Client",
        lambda **kwargs: CapturingHttpClient(captured, **kwargs),
    )
    client = UnlimitedOCRClient(Settings(_env_file=None, data_dir=tmp_path))
    first = tmp_path / "one.png"
    second = tmp_path / "two.png"
    first.write_bytes(b"one")
    second.write_bytes(b"two")

    single_text, single_metadata, single_likelihoods = client._invoke([first])
    multi_text, multi_metadata, multi_likelihoods = client._invoke([first, second])

    assert single_text == "raw OCR"
    assert multi_text == "raw OCR"

    single = captured[0]["payload"]
    multi = captured[1]["payload"]
    assert single["messages"][0]["content"][0]["text"] == "<image>document parsing."
    assert multi["messages"][0]["content"][0]["text"] == "<image>Multi page parsing."
    assert single["skip_special_tokens"] is False
    assert single["max_tokens"] == 4096
    assert multi["max_tokens"] == 8192
    assert single["vllm_xargs"] == {"ngram_size": 35, "window_size": 128}
    assert multi["vllm_xargs"] == {"ngram_size": 35, "window_size": 1024}
    assert len(multi["messages"][0]["content"]) == 3
    assert single["logprobs"] is True
    assert single["top_logprobs"] == 0
    assert single_metadata["finish_reason"] == "stop"
    assert single_metadata["usage"]["completion_tokens"] == 25
    assert single_metadata["page_mode"] == "single_page"
    assert multi_metadata["page_mode"] == "multi_page"
    assert single_likelihoods["available"] is True
    assert single_likelihoods["tokens"][0]["logprob"] == -0.1
    assert multi_likelihoods["alignment_status"] == "aligned"


def test_ocr_client_exposes_vllm_error_message(tmp_path, monkeypatch) -> None:
    class FailingHttpClient:
        def __init__(self, **kwargs: object):
            del kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def post(self, url: str, json: dict[str, object]) -> httpx.Response:
            del json
            request = httpx.Request("POST", url)
            response = httpx.Response(
                400,
                request=request,
                json={"error": {"message": "output token budget is too large"}},
            )
            response.raise_for_status()
            return response

    monkeypatch.setattr("app.services.ocr.httpx.Client", FailingHttpClient)
    client = UnlimitedOCRClient(Settings(_env_file=None, data_dir=tmp_path))
    image = tmp_path / "invoice.png"
    image.write_bytes(b"image")

    with pytest.raises(OCRServiceError, match="output token budget is too large"):
        client._invoke([image])


class ThrottledThenSuccessfulBedrock:
    def __init__(self, result_json: str):
        self.calls = 0
        self.requests: list[dict[str, object]] = []
        self.result_json = result_json

    def converse(self, **request: object) -> dict[str, object]:
        self.calls += 1
        self.requests.append(request)
        if self.calls == 1:
            raise ClientError(
                {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
                "Converse",
            )
        return {
            "stopReason": "end_turn",
            "usage": {"inputTokens": 100, "outputTokens": 50},
            "metrics": {"latencyMs": 500},
            "output": {
                "message": {"content": [{"text": self.result_json}]}
            }
        }


def test_bedrock_client_retries_and_uses_structured_output(
    tmp_path, extraction, monkeypatch
) -> None:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        bedrock_retry_attempts=3,
    )
    service = BedrockInvoiceClient.__new__(BedrockInvoiceClient)
    service.settings = settings
    fake = ThrottledThenSuccessfulBedrock(extraction.model_dump_json())
    service.client = fake
    monkeypatch.setattr("app.services.bedrock.time.sleep", lambda _: None)

    result = service.extract("# Invoice INV-100")

    assert result.invoice.invoice.invoice_number == "INV-100"
    assert result.metadata["stop_reason"] == "end_turn"
    assert result.metadata["usage"]["outputTokens"] == 50
    assert result.metadata["response_shape"]["content_block_count"] == 1
    assert fake.calls == 2
    request = fake.requests[0]
    assert request["modelId"] == "global.anthropic.claude-sonnet-4-6"
    assert request["messages"][0]["content"] == [
        {
            "text": "Translate and extract this OCR Markdown into the required invoice schema:\n\n# Invoice INV-100"
        }
    ]
    schema_text = request["outputConfig"]["textFormat"]["structure"]["jsonSchema"]["schema"]
    schema = json.loads(schema_text)
    assert schema["additionalProperties"] is False
    assert "invoice" in schema["required"]
    assert "field_evidence" in schema["required"]
    evidence_definition = schema["$defs"]["EvidenceReference"]
    assert "field_path" in evidence_definition["required"]
    assert "snippets" in evidence_definition["required"]
    assert "anyOf" not in schema_text
    assert '"title"' not in schema_text


def test_bedrock_client_reports_empty_content_with_response_metadata(
    tmp_path,
) -> None:
    class EmptyBedrock:
        def converse(self, **request: object) -> dict[str, object]:
            del request
            return {
                "stopReason": "max_tokens",
                "usage": {"inputTokens": 500, "outputTokens": 100},
                "output": {"message": {"content": []}},
            }

    service = BedrockInvoiceClient.__new__(BedrockInvoiceClient)
    service.settings = Settings(_env_file=None, data_dir=tmp_path)
    service.client = EmptyBedrock()

    with pytest.raises(BedrockServiceError, match="stopReason=max_tokens") as error:
        service.extract("# Invoice")

    assert error.value.metadata["stop_reason"] == "max_tokens"
    assert error.value.metadata["response_shape"]["content_block_count"] == 0


def test_bedrock_transport_schema_restores_missing_values_to_null(extraction) -> None:
    transport_schema, canonical_schema = _structured_output_schema()
    payload = extraction.model_dump(mode="json")
    payload["supplier"]["address"] = ""
    payload["invoice"]["due_date"] = ""
    payload["line_items"][0]["unit"] = ""

    normalized = _normalize_nullable_strings(
        payload, canonical_schema, canonical_schema
    )
    result = InvoiceExtraction.model_validate(normalized)

    assert transport_schema["$defs"]["Party"]["properties"]["address"] == {
        "type": "string"
    }
    assert result.supplier.address is None
    assert result.invoice.due_date is None
    assert result.line_items[0].unit is None
