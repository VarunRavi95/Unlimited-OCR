"""Compute exact field-value accuracy for labelled PoC invoice results."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

IGNORED_KEYS = {"evidence", "translated_markdown", "warnings", "source_languages"}


def flatten(value: Any, prefix: str = "") -> dict[str, str | None]:
    flattened: dict[str, str | None] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            if key in IGNORED_KEYS:
                continue
            path = f"{prefix}.{key}" if prefix else key
            flattened.update(flatten(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            flattened.update(flatten(item, f"{prefix}[{index}]"))
    else:
        flattened[prefix] = normalize(value)
    return flattened


def normalize(value: Any) -> str | None:
    if value is None:
        return None
    return " ".join(str(value).strip().casefold().split())


def evaluate(labels_path: Path, results_directory: Path) -> dict[str, Any]:
    correct = total = 0
    missing_results: list[str] = []
    mismatches: list[dict[str, Any]] = []
    documents = 0
    for line in labels_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        label = json.loads(line)
        job_id = label["job_id"]
        result_path = results_directory / f"{job_id}.json"
        if not result_path.exists():
            missing_results.append(job_id)
            continue
        documents += 1
        expected = flatten(label["ground_truth"])
        actual = flatten(json.loads(result_path.read_text(encoding="utf-8")))
        for field, expected_value in expected.items():
            total += 1
            actual_value = actual.get(field)
            if actual_value == expected_value:
                correct += 1
            else:
                mismatches.append({"job_id": job_id, "field": field, "expected": expected_value, "actual": actual_value})
    return {"documents_scored": documents, "fields_scored": total, "fields_correct": correct, "accuracy": round(correct / total, 4) if total else 0.0, "missing_results": missing_results, "mismatches": mismatches}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("labels", type=Path, help="JSONL labels with job_id and ground_truth")
    parser.add_argument("results", type=Path, help="Directory containing <job_id>.json result files")
    parser.add_argument("--output", type=Path, default=Path("benchmark-report.json"))
    args = parser.parse_args()
    report = evaluate(args.labels, args.results)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("documents_scored", "fields_scored", "fields_correct", "accuracy")}, indent=2))


if __name__ == "__main__":
    main()

