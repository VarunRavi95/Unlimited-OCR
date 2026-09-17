from __future__ import annotations

import uuid

from app.database import JobRepository
from app.schemas import JobStage, JobStatus


def test_database_create_update_and_recovery(tmp_path) -> None:
    repository = JobRepository(tmp_path / "jobs.db")
    repository.initialize()
    job_id = str(uuid.uuid4())
    created = repository.create(job_id=job_id, filename="invoice.pdf", content_type="application/pdf", source_key=f"incoming/{job_id}/source.pdf", page_count=2, validation_warnings=["low quality"])
    assert created.status == JobStatus.QUEUED
    assert created.validation_warnings == ["low quality"]
    repository.update(job_id, status=JobStatus.PROCESSING, stage=JobStage.OCR)
    assert repository.recover_interrupted() == 1
    recovered = repository.get(job_id)
    assert recovered.status == JobStatus.FAILED
    assert "restart" in recovered.error.lower()
    repository.update(
        job_id,
        trust_json={
            "assessment_version": "1.0",
            "ruleset_version": "1.0.0",
            "calibration_version": None,
            "mode": "signals",
            "document": {
                "status": "review_required",
                "review_reasons": [],
                "critical_fields": [],
            },
            "fields": [],
            "disclaimer": "Test assessment",
            "warnings": [],
        },
    )
    assert repository.get(job_id).trust.mode == "signals"
