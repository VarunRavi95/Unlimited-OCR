from __future__ import annotations

import json
import math
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.config import Settings
from app.schemas import InvoiceExtraction
from app.trust_models import (
    DocumentTrust,
    DocumentTrustStatus,
    EvidenceMatch,
    EvidenceMatchType,
    FieldTrust,
    FieldTrustStatus,
    SignalSeverity,
    TrustAssessment,
    TrustSignal,
)

ASSESSMENT_VERSION = "1.0"
DISCLAIMER = (
    "Evidence status is based on exact OCR support and deterministic checks. "
    "OCR token likelihood is an uncalibrated diagnostic and is not a probability "
    "that a field is correct. A percentage is shown only when a promoted calibration "
    "artifact has passed the configured benchmark gates."
)

_SKIPPED_KEYS = {
    "document_type",
    "source_languages",
    "translated_markdown",
    "description_english",
    "evidence",
    "field_evidence",
    "warnings",
}
_BASE_CRITICAL_FIELDS = {
    "supplier.name",
    "invoice.invoice_number",
    "invoice.invoice_date",
    "invoice.currency",
    "totals.subtotal",
    "totals.tax_total",
    "totals.grand_total",
    "totals.amount_due",
}


def _flatten_scoreable(value: Any, prefix: str = "") -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _SKIPPED_KEYS:
                continue
            path = f"{prefix}.{key}" if prefix else key
            flattened.update(_flatten_scoreable(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            flattened.update(_flatten_scoreable(item, f"{prefix}[{index}]"))
    else:
        flattened[prefix] = value
    return flattened


def _normal(value: Any) -> str:
    return " ".join(str(value).strip().casefold().split())


def _compact(value: Any) -> str:
    return "".join(character for character in _normal(value) if character.isalnum())


def _value_supported(value: Any, snippet: str) -> bool:
    if value is None or value == "":
        return False
    normalized_value = _normal(value)
    normalized_snippet = _normal(snippet)
    if normalized_value and normalized_value in normalized_snippet:
        return True
    compact_value = _compact(value)
    return bool(compact_value) and (
        len(compact_value) >= 2 or compact_value.isdigit()
    ) and compact_value in _compact(snippet)


def _find_matches(markdown: str, snippet: str) -> tuple[EvidenceMatchType, list[tuple[int, int]]]:
    exact: list[tuple[int, int]] = []
    cursor = 0
    while snippet and (index := markdown.find(snippet, cursor)) >= 0:
        exact.append((index, index + len(snippet)))
        cursor = index + max(1, len(snippet))
    if exact:
        return EvidenceMatchType.EXACT, exact
    words = snippet.strip().split()
    if not words:
        return EvidenceMatchType.NONE, []
    pattern = r"\s+".join(re.escape(word) for word in words)
    normalized = [match.span() for match in re.finditer(pattern, markdown, re.IGNORECASE)]
    if normalized:
        return EvidenceMatchType.NORMALIZED, normalized
    return EvidenceMatchType.NONE, []


def _raw_ranges_for_clean_span(
    clean_start: int,
    clean_end: int,
    mapping: list[dict[str, Any]],
) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for span in mapping:
        start = max(clean_start, int(span.get("clean_start", 0)))
        end = min(clean_end, int(span.get("clean_end", 0)))
        if start >= end:
            continue
        ranges.append(
            (
                int(span["raw_start"]) + start - int(span["clean_start"]),
                int(span["raw_start"]) + end - int(span["clean_start"]),
            )
        )
    return ranges


def _likelihood_for_span(
    clean_start: int,
    clean_end: int,
    likelihood_data: dict[str, Any],
) -> dict[str, Any]:
    if not likelihood_data.get("available"):
        return {"alignment_status": likelihood_data.get("alignment_status", "unavailable")}
    raw_ranges = _raw_ranges_for_clean_span(
        clean_start,
        clean_end,
        likelihood_data.get("clean_to_raw_spans", []),
    )
    values: list[float] = []
    for token in likelihood_data.get("tokens", []):
        if token.get("structural") or not isinstance(token.get("logprob"), (int, float)):
            continue
        token_start = int(token.get("raw_start", 0))
        token_end = int(token.get("raw_end", 0))
        if any(token_start < end and token_end > start for start, end in raw_ranges):
            values.append(float(token["logprob"]))
    if not values:
        return {"alignment_status": "no_tokens", "token_count": 0}
    ordered = sorted(values)
    p10 = ordered[max(0, math.ceil(len(ordered) * 0.10) - 1)]
    mean = sum(values) / len(values)
    return {
        "alignment_status": "aligned",
        "geometric_mean_likelihood": round(math.exp(max(mean, -50.0)), 6),
        "p10_logprob": round(p10, 6),
        "minimum_logprob": round(min(values), 6),
        "token_count": len(values),
    }


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    text = re.sub(r"[^0-9,.\-]", "", str(value))
    if not text:
        return None
    if "," in text and "." in text:
        text = text.replace(",", "")
    elif "," in text:
        tail = text.rsplit(",", 1)[-1]
        text = text.replace(",", "." if len(tail) == 2 else "")
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _date(value: Any) -> datetime | None:
    if not value:
        return None
    for pattern in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(str(value).strip(), pattern)
        except ValueError:
            continue
    return None


def _currency_markers(value: Any) -> set[str]:
    text = str(value or "")
    markers = set(re.findall(r"(?<![A-Z])[A-Z]{3}(?![A-Z])", text.upper()))
    for symbol, currency in {"₹": "INR", "€": "EUR", "£": "GBP"}.items():
        if symbol in text:
            markers.add(currency)
    return markers


def _signal(
    code: str,
    passed: bool | None,
    severity: SignalSeverity,
    message: str,
    *,
    observed: Any | None = None,
    expected: Any | None = None,
) -> TrustSignal:
    return TrustSignal(
        code=code,
        passed=passed,
        severity=severity,
        message=message,
        observed=observed,
        expected=expected,
    )


def _deterministic_signals(
    invoice: InvoiceExtraction,
    tolerance: Decimal,
) -> tuple[dict[str, list[TrustSignal]], list[str]]:
    signals: dict[str, list[TrustSignal]] = {}
    document_reasons: list[str] = []

    def add(paths: list[str], signal: TrustSignal) -> None:
        for path in paths:
            signals.setdefault(path, []).append(signal.model_copy(deep=True))

    subtotal = _decimal(invoice.totals.subtotal)
    discount = _decimal(invoice.totals.discount) or Decimal(0)
    shipping = _decimal(invoice.totals.shipping) or Decimal(0)
    tax_total = _decimal(invoice.totals.tax_total) or Decimal(0)
    grand_total = _decimal(invoice.totals.grand_total)
    if subtotal is not None and grand_total is not None:
        expected = subtotal - discount + shipping + tax_total
        passed = abs(expected - grand_total) <= tolerance
        add(
            ["totals.subtotal", "totals.tax_total", "totals.grand_total"],
            _signal(
                "totals_arithmetic",
                passed,
                SignalSeverity.INFO if passed else SignalSeverity.ERROR,
                "Invoice totals reconcile." if passed else "Subtotal, adjustments, tax, and grand total do not reconcile.",
                observed=str(grand_total),
                expected=str(expected),
            ),
        )
        if not passed:
            document_reasons.append("Invoice totals do not reconcile.")

    amount_due = _decimal(invoice.totals.amount_due)
    if amount_due is not None and grand_total is not None:
        passed = amount_due <= grand_total + tolerance
        add(
            ["totals.amount_due"],
            _signal(
                "amount_due_range",
                passed,
                SignalSeverity.INFO if passed else SignalSeverity.ERROR,
                "Amount due is not greater than the grand total." if passed else "Amount due is greater than the grand total.",
                observed=str(amount_due),
                expected=f"<= {grand_total}",
            ),
        )
        if not passed:
            document_reasons.append("Amount due exceeds the grand total.")

    tax_amounts: list[Decimal] = []
    for index, tax in enumerate(invoice.totals.taxes):
        base = _decimal(tax.base_amount)
        rate = _decimal(tax.rate)
        amount = _decimal(tax.amount)
        if amount is not None:
            tax_amounts.append(amount)
        if base is None or rate is None or amount is None:
            continue
        expected = base * rate / Decimal(100)
        passed = abs(expected - amount) <= tolerance
        paths = [
            f"totals.taxes[{index}].base_amount",
            f"totals.taxes[{index}].rate",
            f"totals.taxes[{index}].amount",
        ]
        add(
            paths,
            _signal(
                "tax_arithmetic",
                passed,
                SignalSeverity.INFO if passed else SignalSeverity.ERROR,
                "Tax base, rate, and amount reconcile."
                if passed
                else "Tax base, rate, and amount do not reconcile.",
                observed=str(amount),
                expected=str(expected),
            ),
        )
        if not passed:
            document_reasons.append(f"Tax entry {index + 1} does not reconcile.")

    if tax_amounts and _decimal(invoice.totals.tax_total) is not None:
        extracted_tax_total = _decimal(invoice.totals.tax_total)
        assert extracted_tax_total is not None
        expected_tax_total = sum(tax_amounts, Decimal(0))
        passed = abs(expected_tax_total - extracted_tax_total) <= tolerance
        tax_paths = ["totals.tax_total"] + [
            f"totals.taxes[{index}].amount"
            for index in range(len(invoice.totals.taxes))
        ]
        add(
            tax_paths,
            _signal(
                "tax_total_reconciliation",
                passed,
                SignalSeverity.INFO if passed else SignalSeverity.ERROR,
                "Tax entries reconcile with the tax total."
                if passed
                else "Tax entries do not reconcile with the tax total.",
                observed=str(extracted_tax_total),
                expected=str(expected_tax_total),
            ),
        )
        if not passed:
            document_reasons.append("Tax entries do not reconcile with the tax total.")

    invoice_currency = (invoice.invoice.currency or "").strip().upper()
    monetary_values: dict[str, Any] = {
        "totals.subtotal": invoice.totals.subtotal,
        "totals.discount": invoice.totals.discount,
        "totals.shipping": invoice.totals.shipping,
        "totals.tax_total": invoice.totals.tax_total,
        "totals.grand_total": invoice.totals.grand_total,
        "totals.amount_due": invoice.totals.amount_due,
    }
    for index, item in enumerate(invoice.line_items):
        monetary_values[f"line_items[{index}].unit_price"] = item.unit_price
        monetary_values[f"line_items[{index}].tax_amount"] = item.tax_amount
        monetary_values[f"line_items[{index}].line_total"] = item.line_total
    for index, tax in enumerate(invoice.totals.taxes):
        monetary_values[f"totals.taxes[{index}].base_amount"] = tax.base_amount
        monetary_values[f"totals.taxes[{index}].amount"] = tax.amount
    if invoice_currency:
        for path, value in monetary_values.items():
            markers = _currency_markers(value)
            contradictory = sorted(marker for marker in markers if marker != invoice_currency)
            if not contradictory:
                continue
            message = (
                f"Amount contains currency marker(s) {', '.join(contradictory)} "
                f"but invoice currency is {invoice_currency}."
            )
            add(
                [path, "invoice.currency"],
                _signal(
                    "currency_consistency",
                    False,
                    SignalSeverity.ERROR,
                    message,
                    observed=", ".join(contradictory),
                    expected=invoice_currency,
                ),
            )
            document_reasons.append(message)

    line_totals: list[Decimal] = []
    for index, item in enumerate(invoice.line_items):
        path = f"line_items[{index}].line_total"
        quantity = _decimal(item.quantity)
        unit_price = _decimal(item.unit_price)
        line_total = _decimal(item.line_total)
        tax = _decimal(item.tax_amount) or Decimal(0)
        if line_total is not None:
            line_totals.append(line_total)
        if quantity is not None and unit_price is not None and line_total is not None:
            base = quantity * unit_price
            passed = min(abs(base - line_total), abs(base + tax - line_total)) <= tolerance
            add(
                [path, f"line_items[{index}].quantity", f"line_items[{index}].unit_price"],
                _signal(
                    "line_arithmetic",
                    passed,
                    SignalSeverity.INFO if passed else SignalSeverity.WARNING,
                    "Line quantity and price reconcile." if passed else "Line quantity, price, tax, and total need review.",
                    observed=str(line_total),
                    expected=f"{base} before tax",
                ),
            )
    if line_totals and (subtotal is not None or grand_total is not None):
        line_sum = sum(line_totals, Decimal(0))
        candidates = [value for value in (subtotal, grand_total) if value is not None]
        passed = any(abs(line_sum - candidate) <= tolerance for candidate in candidates)
        signal = _signal(
            "line_sum",
            passed,
            SignalSeverity.INFO if passed else SignalSeverity.WARNING,
            "Line totals reconcile with an invoice total." if passed else "The sum of line totals does not match subtotal or grand total.",
            observed=str(line_sum),
            expected=" or ".join(str(value) for value in candidates),
        )
        add([f"line_items[{index}].line_total" for index in range(len(invoice.line_items))], signal)
        if not passed:
            document_reasons.append("Line totals need reconciliation review.")

    invoice_date = _date(invoice.invoice.invoice_date)
    due_date = _date(invoice.invoice.due_date)
    if invoice_date and due_date:
        passed = due_date >= invoice_date
        add(
            ["invoice.invoice_date", "invoice.due_date"],
            _signal(
                "date_order",
                passed,
                SignalSeverity.INFO if passed else SignalSeverity.ERROR,
                "Due date follows invoice date." if passed else "Due date is earlier than invoice date.",
            ),
        )
        if not passed:
            document_reasons.append("Due date is earlier than invoice date.")
    return signals, document_reasons


class CalibrationRuntime:
    def __init__(self, path: Path | None):
        self.artifact: dict[str, Any] | None = None
        self.warning: str | None = None
        if path is None:
            return
        try:
            artifact = json.loads(path.read_text(encoding="utf-8"))
            if not artifact.get("promoted"):
                self.warning = "Calibration artifact is present but has not passed promotion gates."
                return
            self.artifact = artifact
        except Exception as error:
            self.warning = f"Calibration artifact could not be loaded: {error}"

    @property
    def version(self) -> str | None:
        return str(self.artifact.get("version")) if self.artifact else None

    def predict(self, features: dict[str, float]) -> float | None:
        if not self.artifact:
            return None
        names = self.artifact.get("feature_names", [])
        means = self.artifact.get("means", {})
        scales = self.artifact.get("scales", {})
        coefficients = self.artifact.get("coefficients", {})
        value = float(self.artifact.get("intercept", 0.0))
        for name in names:
            scale = float(scales.get(name, 1.0)) or 1.0
            standardized = (float(features.get(name, 0.0)) - float(means.get(name, 0.0))) / scale
            value += standardized * float(coefficients.get(name, 0.0))
        probability = 1.0 / (1.0 + math.exp(-max(-50.0, min(50.0, value))))
        isotonic = self.artifact.get("isotonic", {})
        xs, ys = isotonic.get("x", []), isotonic.get("y", [])
        if xs and len(xs) == len(ys):
            for index, boundary in enumerate(xs):
                if probability <= float(boundary):
                    return round(float(ys[index]), 6)
            return round(float(ys[-1]), 6)
        return round(probability, 6)


class TrustAssessor:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.calibration = CalibrationRuntime(settings.trust_calibration_path)

    def assess(
        self,
        invoice: InvoiceExtraction,
        *,
        clean_markdown: str,
        likelihood_data: dict[str, Any] | None,
        validation_warnings: list[str],
        ocr_metadata: dict[str, Any],
        page_count: int,
    ) -> TrustAssessment:
        likelihood_data = likelihood_data or {}
        values = _flatten_scoreable(invoice.model_dump(mode="json"))
        evidence_by_path = {
            reference.field_path: reference.snippets for reference in invoice.field_evidence
        }
        validation, document_reasons = _deterministic_signals(
            invoice,
            Decimal(str(self.settings.trust_amount_tolerance)),
        )
        source_warning = bool(validation_warnings)
        truncated = ocr_metadata.get("finish_reason") in {"length", "max_tokens"}
        fields: list[FieldTrust] = []
        critical_paths = set(_BASE_CRITICAL_FIELDS)
        critical_paths.update(
            path for path in values if re.fullmatch(r"line_items\[\d+\]\.line_total", path)
        )

        for path, value in values.items():
            critical = path in critical_paths
            field_signals = list(validation.get(path, []))
            matches: list[EvidenceMatch] = []
            for snippet in evidence_by_path.get(path, []):
                match_type, positions = _find_matches(clean_markdown, snippet)
                first = positions[0] if positions else (None, None)
                likelihood = (
                    _likelihood_for_span(first[0], first[1], likelihood_data)
                    if first[0] is not None and first[1] is not None
                    else {"alignment_status": "unavailable"}
                )
                matches.append(
                    EvidenceMatch(
                        snippet=snippet,
                        match_type=match_type,
                        value_supported=_value_supported(value, snippet),
                        occurrences=len(positions),
                        clean_start=first[0],
                        clean_end=first[1],
                        **likelihood,
                    )
                )

            reasons: list[str] = []
            calibrated: float | None = None
            diagnostic_values = [
                match.geometric_mean_likelihood
                for match in matches
                if match.geometric_mean_likelihood is not None
            ]
            diagnostic = min(diagnostic_values) if diagnostic_values else None
            if value is None or value == "":
                status = FieldTrustStatus.NOT_ASSESSED
                if critical:
                    reasons.append("Critical AP field was not extracted.")
            elif not matches or all(match.match_type == EvidenceMatchType.NONE for match in matches):
                status = FieldTrustStatus.UNSUPPORTED
                reasons.append("No assigned evidence snippet was found in clean OCR Markdown.")
            elif not any(match.value_supported for match in matches):
                status = FieldTrustStatus.UNSUPPORTED
                reasons.append("Assigned OCR evidence does not contain the extracted value.")
            elif any(match.match_type == EvidenceMatchType.NORMALIZED for match in matches):
                status = FieldTrustStatus.REVIEW
                reasons.append("Evidence matched only after case or whitespace normalization.")
            elif any((match.occurrences or 0) > 1 for match in matches):
                status = FieldTrustStatus.REVIEW
                reasons.append("Evidence occurs more than once and is ambiguous.")
            else:
                status = FieldTrustStatus.SUPPORTED

            if status == FieldTrustStatus.SUPPORTED and source_warning:
                status = FieldTrustStatus.REVIEW
                reasons.append("Source-quality warning applies to this document.")
            if status == FieldTrustStatus.SUPPORTED and truncated:
                status = FieldTrustStatus.REVIEW
                reasons.append("OCR stopped at its output-token limit.")
            for signal in field_signals:
                if signal.passed is False and signal.severity == SignalSeverity.ERROR:
                    status = FieldTrustStatus.UNSUPPORTED
                    reasons.append(signal.message)
                elif signal.passed is False and status == FieldTrustStatus.SUPPORTED:
                    status = FieldTrustStatus.REVIEW
                    reasons.append(signal.message)

            if self.settings.trust_mode == "calibrated" and value not in {None, ""}:
                feature_map = self._features(
                    path,
                    matches,
                    field_signals,
                    source_warning,
                    truncated,
                    page_count,
                )
                calibrated = self.calibration.predict(feature_map)
                if calibrated is not None:
                    if calibrated < self.settings.trust_review_threshold:
                        status = FieldTrustStatus.UNSUPPORTED
                        reasons.append("Calibrated correctness is below the review threshold.")
                    elif calibrated < self.settings.trust_supported_threshold and status == FieldTrustStatus.SUPPORTED:
                        status = FieldTrustStatus.REVIEW
                        reasons.append("Calibrated correctness is below the supported threshold.")

            fields.append(
                FieldTrust(
                    field_path=path,
                    value=value,
                    status=status,
                    critical=critical,
                    calibrated_confidence=calibrated,
                    diagnostic_likelihood=diagnostic,
                    evidence_matches=matches,
                    validation_signals=field_signals,
                    review_reasons=list(dict.fromkeys(reasons)),
                )
            )

        by_path = {field.field_path: field for field in fields}
        for critical_path in sorted(critical_paths):
            field = by_path.get(critical_path)
            if field is None or field.status == FieldTrustStatus.NOT_ASSESSED:
                document_reasons.append(f"Critical field {critical_path} is missing.")
        critical_fields = [by_path[path] for path in critical_paths if path in by_path]
        if invoice.document_type.casefold() != "invoice":
            document_status = DocumentTrustStatus.INSUFFICIENT_SUPPORT
            document_reasons.append("The document was not classified as an invoice.")
        elif any(field.status == FieldTrustStatus.UNSUPPORTED for field in critical_fields):
            document_status = DocumentTrustStatus.INSUFFICIENT_SUPPORT
            document_reasons.append("At least one critical field is unsupported.")
        elif any(
            field.status in {FieldTrustStatus.REVIEW, FieldTrustStatus.NOT_ASSESSED}
            for field in critical_fields
        ) or any(path not in by_path for path in critical_paths):
            document_status = DocumentTrustStatus.REVIEW_REQUIRED
            document_reasons.append("At least one critical field requires review.")
        else:
            document_status = DocumentTrustStatus.READY

        assessment_warnings = list(validation_warnings)
        likelihood_warning = likelihood_data.get("warning")
        if likelihood_warning:
            assessment_warnings.append(str(likelihood_warning))
        if self.settings.trust_mode == "calibrated" and self.calibration.warning:
            assessment_warnings.append(self.calibration.warning)
        return TrustAssessment(
            assessment_version=ASSESSMENT_VERSION,
            ruleset_version=self.settings.trust_ruleset_version,
            calibration_version=self.calibration.version,
            mode=self.settings.trust_mode,
            document=DocumentTrust(
                status=document_status,
                review_reasons=list(dict.fromkeys(document_reasons)),
                critical_fields=sorted(critical_paths),
            ),
            fields=fields,
            disclaimer=DISCLAIMER,
            warnings=list(dict.fromkeys(assessment_warnings)),
        )

    @staticmethod
    def _features(
        path: str,
        matches: list[EvidenceMatch],
        signals: list[TrustSignal],
        source_warning: bool,
        truncated: bool,
        page_count: int,
    ) -> dict[str, float]:
        log_likelihoods = [
            math.log(max(match.geometric_mean_likelihood or 0.0, 1e-12))
            for match in matches
            if match.geometric_mean_likelihood is not None
        ]
        p10_values = [match.p10_logprob for match in matches if match.p10_logprob is not None]
        minimums = [match.minimum_logprob for match in matches if match.minimum_logprob is not None]
        family = path.split(".", 1)[0].split("[", 1)[0]
        features = {
            "mean_logprob": sum(log_likelihoods) / len(log_likelihoods) if log_likelihoods else -20.0,
            "p10_logprob": min(p10_values) if p10_values else -20.0,
            "minimum_logprob": min(minimums) if minimums else -20.0,
            "token_count_log": math.log1p(sum(match.token_count for match in matches)),
            "exact_match": float(any(match.match_type == EvidenceMatchType.EXACT for match in matches)),
            "normalized_match": float(any(match.match_type == EvidenceMatchType.NORMALIZED for match in matches)),
            "ambiguous": float(any((match.occurrences or 0) > 1 for match in matches)),
            "value_supported": float(any(match.value_supported for match in matches)),
            "validation_errors": float(sum(signal.passed is False and signal.severity == SignalSeverity.ERROR for signal in signals)),
            "validation_warnings": float(sum(signal.passed is False and signal.severity == SignalSeverity.WARNING for signal in signals)),
            "source_warning": float(source_warning),
            "ocr_truncated": float(truncated),
            "page_count_log": math.log1p(page_count),
        }
        for name in ("supplier", "buyer", "invoice", "totals", "taxes", "line_items", "payment"):
            features[f"family_{name}"] = float(family == name)
        return features
