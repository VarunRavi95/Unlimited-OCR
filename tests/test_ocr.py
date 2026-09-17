from app.services.ocr import (
    _token_likelihood_data,
    clean_ocr_output,
    clean_ocr_output_with_alignment,
)


def test_clean_ocr_output_removes_grounding_and_images() -> None:
    raw = """<|det|>text [1,2,3,4]<|/det|><|ref|>Invoice 100<|/ref|>
continued line
<|det|>image [4,5,6,7]<|/det|>ignored
<|det|>table [8,9,10,11]<|/det|>| Total | 118.00 |
"""
    clean = clean_ocr_output(raw)
    assert "<|det|>" not in clean
    assert "Invoice 100" in clean
    assert "continued line" in clean
    assert "ignored" not in clean
    assert "| Total | 118.00 |" in clean


def test_clean_alignment_preserves_unicode_source_positions() -> None:
    raw = "<|det|>text [1,2,3,4]<|/det|><|ref|>इनवॉइस १००<|/ref|>\nTotal 118.00"
    clean, spans = clean_ocr_output_with_alignment(raw)

    assert clean == "इनवॉइस १००\nTotal 118.00"
    invoice_start = clean.index("इनवॉइस")
    raw_start = raw.index("इनवॉइस")
    matching = next(span for span in spans if span["clean_start"] == invoice_start)
    assert matching["raw_start"] == raw_start


def test_token_likelihood_reconstructs_utf8_and_rejects_misalignment() -> None:
    raw = "इनवॉइस 100"
    choice = {
        "logprobs": {
            "content": [
                {
                    "token": raw,
                    "bytes": list(raw.encode("utf-8")),
                    "logprob": -0.25,
                }
            ]
        }
    }
    aligned = _token_likelihood_data(raw, choice)
    assert aligned["available"] is True
    assert aligned["tokens"][0]["raw_end"] == len(raw)

    mismatched = _token_likelihood_data("different", choice)
    assert mismatched["available"] is False
    assert mismatched["alignment_status"] == "failed"


def test_missing_logprobs_are_explicitly_unavailable() -> None:
    likelihoods = _token_likelihood_data("OCR", {})
    assert likelihoods["available"] is False
    assert "did not return" in likelihoods["warning"]
