"""Train and evaluate the PoC field-correctness calibration artifact.

The trainer uses only the Python standard library. It fits a standardized
logistic model on the training document split and a monotonic isotonic mapping
on the calibration split. The held-out split determines whether the artifact is
promoted for customer-facing percentages.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services.trust import TrustAssessor
from app.trust_models import TrustAssessment

IGNORED_KEYS = {
    "document_type",
    "source_languages",
    "translated_markdown",
    "description_english",
    "evidence",
    "field_evidence",
    "warnings",
}


@dataclass
class Sample:
    job_id: str
    field_path: str
    correct: int
    features: dict[str, float]
    critical: bool
    language: str
    quality_bucket: str


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    output: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            if key in IGNORED_KEYS:
                continue
            path = f"{prefix}.{key}" if prefix else key
            output.update(flatten(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            output.update(flatten(item, f"{prefix}[{index}]"))
    else:
        output[prefix] = value
    return output


def normalize(value: Any) -> str | None:
    if value is None:
        return None
    return " ".join(str(value).strip().casefold().split())


def load_samples(labels_path: Path, results_dir: Path, trust_dir: Path) -> list[Sample]:
    samples: list[Sample] = []
    for line in labels_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        label = json.loads(line)
        job_id = label["job_id"]
        result_path = results_dir / f"{job_id}.json"
        trust_path = trust_dir / f"{job_id}.json"
        if not result_path.exists() or not trust_path.exists():
            continue
        expected = flatten(label["ground_truth"])
        actual = flatten(json.loads(result_path.read_text(encoding="utf-8")))
        assessment = TrustAssessment.model_validate_json(
            trust_path.read_text(encoding="utf-8")
        )
        fields = {field.field_path: field for field in assessment.fields}
        metadata = label.get("metadata", {})
        language = str(metadata.get("language", "unknown"))
        quality = str(metadata.get("quality_bucket", "unknown"))
        page_count = int(metadata.get("page_count", 1))
        source_warning = bool(assessment.warnings)
        truncated = any("token limit" in warning.casefold() for warning in assessment.warnings)
        for path, expected_value in expected.items():
            field = fields.get(path)
            if field is None:
                continue
            features = TrustAssessor._features(
                path,
                field.evidence_matches,
                field.validation_signals,
                source_warning,
                truncated,
                page_count,
            )
            samples.append(
                Sample(
                    job_id=job_id,
                    field_path=path,
                    correct=int(normalize(actual.get(path)) == normalize(expected_value)),
                    features=features,
                    critical=field.critical,
                    language=language,
                    quality_bucket=quality,
                )
            )
    return samples


def split_documents(samples: list[Sample], seed: int = 41) -> tuple[list[Sample], list[Sample], list[Sample]]:
    buckets: dict[tuple[str, str], list[str]] = {}
    sample_by_job: dict[str, list[Sample]] = {}
    for sample in samples:
        sample_by_job.setdefault(sample.job_id, []).append(sample)
        buckets.setdefault((sample.language, sample.quality_bucket), [])
        if sample.job_id not in buckets[(sample.language, sample.quality_bucket)]:
            buckets[(sample.language, sample.quality_bucket)].append(sample.job_id)
    assignments: dict[str, str] = {}
    for bucket, job_ids in sorted(buckets.items()):
        rng = random.Random(f"{seed}:{bucket[0]}:{bucket[1]}")
        rng.shuffle(job_ids)
        for index, job_id in enumerate(job_ids):
            fraction = index / max(1, len(job_ids))
            assignments.setdefault(
                job_id,
                "train" if fraction < 0.60 else "calibration" if fraction < 0.80 else "test",
            )
    return tuple(
        [sample for sample in samples if assignments.get(sample.job_id) == split]
        for split in ("train", "calibration", "test")
    )  # type: ignore[return-value]


def _matrix(samples: list[Sample], names: list[str]) -> list[list[float]]:
    return [[float(sample.features.get(name, 0.0)) for name in names] for sample in samples]


def fit_logistic(samples: list[Sample], iterations: int = 2500, learning_rate: float = 0.05) -> dict[str, Any]:
    names = sorted({name for sample in samples for name in sample.features})
    matrix = _matrix(samples, names)
    means = [sum(row[index] for row in matrix) / len(matrix) for index in range(len(names))]
    scales = []
    for index, mean in enumerate(means):
        variance = sum((row[index] - mean) ** 2 for row in matrix) / len(matrix)
        scales.append(math.sqrt(variance) or 1.0)
    standardized = [
        [(value - means[index]) / scales[index] for index, value in enumerate(row)]
        for row in matrix
    ]
    weights = [0.0] * len(names)
    positive_rate = min(1 - 1e-6, max(1e-6, sum(sample.correct for sample in samples) / len(samples)))
    intercept = math.log(positive_rate / (1 - positive_rate))
    l2 = 0.001
    for _ in range(iterations):
        gradient = [0.0] * len(weights)
        intercept_gradient = 0.0
        for row, sample in zip(standardized, samples, strict=True):
            linear = intercept + sum(weight * value for weight, value in zip(weights, row, strict=True))
            probability = 1.0 / (1.0 + math.exp(-max(-50.0, min(50.0, linear))))
            error = probability - sample.correct
            intercept_gradient += error
            for index, value in enumerate(row):
                gradient[index] += error * value
        count = len(samples)
        intercept -= learning_rate * intercept_gradient / count
        for index in range(len(weights)):
            weights[index] -= learning_rate * (gradient[index] / count + l2 * weights[index])
    return {
        "feature_names": names,
        "means": dict(zip(names, means, strict=True)),
        "scales": dict(zip(names, scales, strict=True)),
        "coefficients": dict(zip(names, weights, strict=True)),
        "intercept": intercept,
    }


def logistic_predict(model: dict[str, Any], features: dict[str, float]) -> float:
    value = float(model["intercept"])
    for name in model["feature_names"]:
        scale = float(model["scales"][name]) or 1.0
        standardized = (float(features.get(name, 0.0)) - float(model["means"][name])) / scale
        value += standardized * float(model["coefficients"][name])
    return 1.0 / (1.0 + math.exp(-max(-50.0, min(50.0, value))))


def fit_isotonic(probabilities: list[float], labels: list[int]) -> dict[str, list[float]]:
    points = sorted(zip(probabilities, labels, strict=True))
    blocks: list[dict[str, float]] = []
    for probability, label in points:
        blocks.append({"min": probability, "max": probability, "sum": float(label), "count": 1.0})
        while len(blocks) >= 2:
            previous, current = blocks[-2], blocks[-1]
            if previous["sum"] / previous["count"] <= current["sum"] / current["count"]:
                break
            blocks[-2:] = [{
                "min": previous["min"],
                "max": current["max"],
                "sum": previous["sum"] + current["sum"],
                "count": previous["count"] + current["count"],
            }]
    return {
        "x": [block["max"] for block in blocks],
        "y": [block["sum"] / block["count"] for block in blocks],
    }


def isotonic_predict(mapping: dict[str, list[float]], probability: float) -> float:
    for boundary, value in zip(mapping["x"], mapping["y"], strict=True):
        if probability <= boundary:
            return value
    return mapping["y"][-1] if mapping["y"] else probability


def metrics(samples: list[Sample], probabilities: list[float], baseline_rate: float) -> dict[str, Any]:
    if not samples:
        return {}
    brier = sum((probability - sample.correct) ** 2 for sample, probability in zip(samples, probabilities, strict=True)) / len(samples)
    baseline_brier = sum((baseline_rate - sample.correct) ** 2 for sample in samples) / len(samples)
    ece = 0.0
    for bin_index in range(10):
        low, high = bin_index / 10, (bin_index + 1) / 10
        bucket = [
            (sample, probability)
            for sample, probability in zip(samples, probabilities, strict=True)
            if low <= probability < high or (bin_index == 9 and probability == 1.0)
        ]
        if bucket:
            accuracy = sum(sample.correct for sample, _ in bucket) / len(bucket)
            confidence = sum(probability for _, probability in bucket) / len(bucket)
            ece += len(bucket) / len(samples) * abs(accuracy - confidence)
    high = [
        sample.correct
        for sample, probability in zip(samples, probabilities, strict=True)
        if sample.critical and probability >= 0.90
    ]
    incorrect = [
        probability
        for sample, probability in zip(samples, probabilities, strict=True)
        if sample.critical and not sample.correct
    ]
    return {
        "samples": len(samples),
        "documents": len({sample.job_id for sample in samples}),
        "ece": round(ece, 6),
        "brier": round(brier, 6),
        "baseline_brier": round(baseline_brier, 6),
        "high_confidence_critical_accuracy": round(sum(high) / len(high), 6) if high else None,
        "high_confidence_critical_count": len(high),
        "incorrect_critical_review_recall": round(sum(probability < 0.90 for probability in incorrect) / len(incorrect), 6) if incorrect else None,
        "incorrect_critical_count": len(incorrect),
    }


def grouped_metrics(samples: list[Sample], probabilities: list[float], baseline: float, attribute: str) -> dict[str, Any]:
    output: dict[str, Any] = {}
    values = sorted({getattr(sample, attribute) for sample in samples})
    for value in values:
        pairs = [
            (sample, probability)
            for sample, probability in zip(samples, probabilities, strict=True)
            if getattr(sample, attribute) == value
        ]
        output[value] = metrics([sample for sample, _ in pairs], [probability for _, probability in pairs], baseline)
    return output


def dataset_fingerprint(labels_path: Path, samples: list[Sample]) -> str:
    digest = hashlib.sha256(labels_path.read_bytes())
    digest.update("|".join(sorted({sample.job_id for sample in samples})).encode())
    return digest.hexdigest()


def train(labels: Path, results: Path, trust: Path, version: str) -> dict[str, Any]:
    samples = load_samples(labels, results, trust)
    train_samples, calibration_samples, test_samples = split_documents(samples)
    if not train_samples or not calibration_samples or not test_samples:
        raise ValueError("Calibration requires non-empty 60/20/20 document splits.")
    model = fit_logistic(train_samples)
    calibration_probabilities = [logistic_predict(model, sample.features) for sample in calibration_samples]
    isotonic = fit_isotonic(calibration_probabilities, [sample.correct for sample in calibration_samples])
    test_probabilities = [
        isotonic_predict(isotonic, logistic_predict(model, sample.features)) for sample in test_samples
    ]
    baseline = sum(sample.correct for sample in train_samples) / len(train_samples)
    held_out = metrics(test_samples, test_probabilities, baseline)
    high_accuracy = held_out.get("high_confidence_critical_accuracy")
    review_recall = held_out.get("incorrect_critical_review_recall")
    gates = {
        "ece_lte_0_10": held_out.get("ece", 1.0) <= 0.10,
        "brier_better_than_baseline": held_out.get("brier", 1.0) < held_out.get("baseline_brier", 0.0),
        "high_confidence_critical_accuracy_gte_0_95": high_accuracy is not None and high_accuracy >= 0.95,
        "incorrect_critical_review_recall_gte_0_80": review_recall is not None and review_recall >= 0.80,
    }
    return {
        "schema_version": 1,
        "version": version,
        "promoted": all(gates.values()),
        "dataset_fingerprint": dataset_fingerprint(labels, samples),
        "split": {"train": 0.60, "calibration": 0.20, "test": 0.20, "seed": 41},
        **model,
        "isotonic": isotonic,
        "metrics": {
            "held_out": held_out,
            "by_language": grouped_metrics(test_samples, test_probabilities, baseline, "language"),
            "by_quality_bucket": grouped_metrics(test_samples, test_probabilities, baseline, "quality_bucket"),
            "promotion_gates": gates,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("labels", type=Path)
    parser.add_argument("results", type=Path)
    parser.add_argument("trust", type=Path)
    parser.add_argument("--output", type=Path, default=Path("benchmark/calibration.json"))
    parser.add_argument("--version", default="calibration-v1")
    args = parser.parse_args()
    artifact = train(args.labels, args.results, args.trust, args.version)
    args.output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"promoted": artifact["promoted"], **artifact["metrics"]["held_out"]}, indent=2))


if __name__ == "__main__":
    main()
