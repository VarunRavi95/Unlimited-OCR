from __future__ import annotations

import gzip
import json
import uuid

from app.config import Settings
from app.database import JobRepository
from app.schemas import JobStage, JobStatus
from app.services.bedrock import BedrockResult
from app.services.ocr import OCRResult
from app.services.processor import JobProcessor
from app.services.storage import LocalStorage


class FakeOCR:
    def __init__(self):
        self.calls = 0

    def extract(self, source: bytes, content_type: str) -> OCRResult:
        self.calls += 1
        assert source and content_type == "image/png"
        return OCRResult(
            "raw OCR",
            "# Invoice INV-100",
            1,
            {
                "finish_reason": "stop",
                "usage": {"completion_tokens": 50},
                "max_tokens_requested": 4096,
            },
            {
                "schema_version": 1,
                "available": False,
                "alignment_status": "unavailable",
                "warning": "Mocked response did not include token likelihoods.",
                "tokens": [],
                "clean_to_raw_spans": [],
            },
        )

    def clear_temporary_state(self) -> None:
        return None


class FlakyOCR(FakeOCR):
    def __init__(self):
        super().__init__()
        self.clear_calls = 0

    def extract(self, source: bytes, content_type: str) -> OCRResult:
        if self.calls == 0:
            self.calls += 1
            raise RuntimeError("temporary GPU failure")
        return super().extract(source, content_type)

    def clear_temporary_state(self) -> None:
        self.clear_calls += 1


class FakeBedrock:
    def __init__(self, result):
        self.result = result

    def extract(self, markdown: str):
        assert "INV-100" in markdown
        return BedrockResult(
            invoice=self.result.model_copy(deep=True),
            metadata={
                "stop_reason": "end_turn",
                "usage": {"outputTokens": 100},
                "response_shape": {"content_block_count": 1},
            },
        )


class FailingBedrock:
    def extract(self, markdown: str):
        raise RuntimeError("simulated Bedrock failure")


def build_job(tmp_path, validation_warnings=None):
    settings = Settings(_env_file=None, data_dir=tmp_path, storage_backend="local", worker_enabled=False)
    repository = JobRepository(settings.database_path)
    repository.initialize()
    storage = LocalStorage(settings.local_storage_path)
    job_id = str(uuid.uuid4())
    source_key = f"incoming/{job_id}/source.png"
    storage.put_bytes(source_key, b"image", "image/png")
    repository.create(job_id=job_id, filename="invoice.png", content_type="image/png", source_key=source_key, page_count=1, validation_warnings=validation_warnings or [])
    return settings, repository, storage, job_id


def test_processor_writes_all_artifacts(tmp_path, extraction) -> None:
    settings, repository, storage, job_id = build_job(tmp_path, ["DPI unavailable"])
    processor = JobProcessor(settings=settings, repository=repository, storage=storage, ocr_client=FakeOCR(), bedrock_client=FakeBedrock(extraction))
    processor.process(job_id)
    job = repository.get(job_id)
    assert job.status == JobStatus.COMPLETED
    assert set(job.artifacts) == {
        "raw",
        "clean",
        "ocr_metadata",
        "ocr_likelihoods",
        "result",
        "trust",
        "manifest",
    }
    assert job.result["warnings"][0] == "DPI unavailable"
    for key in job.artifacts.values():
        assert storage.exists(key)
    manifest = json.loads(storage.get_bytes(job.artifacts["manifest"]))
    assert manifest["status"] == "completed"
    assert manifest["models"]["ocr_revision"] == settings.ocr_model_revision
    assert manifest["inference"]["ocr"]["finish_reason"] == "stop"
    assert manifest["inference"]["bedrock"]["stop_reason"] == "end_turn"
    assert manifest["trust"]["available"] is True
    assert job.trust is not None
    likelihoods = json.loads(
        gzip.decompress(storage.get_bytes(job.artifacts["ocr_likelihoods"]))
    )
    assert likelihoods["available"] is False


def test_processor_failure_is_visible_and_retryable(tmp_path) -> None:
    settings, repository, storage, job_id = build_job(tmp_path)
    processor = JobProcessor(settings=settings, repository=repository, storage=storage, ocr_client=FakeOCR(), bedrock_client=FailingBedrock())
    processor.process(job_id)
    job = repository.get(job_id)
    assert job.status == JobStatus.FAILED
    assert "simulated Bedrock failure" in job.error
    assert storage.exists(job.artifacts["manifest"])


def test_processor_retries_ocr_once_after_clearing_state(
    tmp_path, extraction, monkeypatch
) -> None:
    settings, repository, storage, job_id = build_job(tmp_path)
    ocr = FlakyOCR()
    monkeypatch.setattr("app.services.processor.time.sleep", lambda _: None)
    processor = JobProcessor(
        settings=settings,
        repository=repository,
        storage=storage,
        ocr_client=ocr,
        bedrock_client=FakeBedrock(extraction),
    )

    processor.process(job_id)

    assert repository.get(job_id).status == JobStatus.COMPLETED
    assert ocr.calls == 2
    assert ocr.clear_calls == 1


def test_processor_resumes_at_bedrock_when_ocr_artifacts_exist(
    tmp_path, extraction
) -> None:
    settings, repository, storage, job_id = build_job(tmp_path)
    ocr = FakeOCR()
    failing = JobProcessor(
        settings=settings,
        repository=repository,
        storage=storage,
        ocr_client=ocr,
        bedrock_client=FailingBedrock(),
    )
    failing.process(job_id)

    failed_job = repository.get(job_id)
    assert failed_job.status == JobStatus.FAILED
    assert ocr.calls == 1
    repository.update(
        job_id,
        status=JobStatus.QUEUED,
        stage=JobStage.BEDROCK,
        completed_at=None,
        error=None,
    )

    resumed = JobProcessor(
        settings=settings,
        repository=repository,
        storage=storage,
        ocr_client=ocr,
        bedrock_client=FakeBedrock(extraction),
    )
    resumed.process(job_id)

    job = repository.get(job_id)
    assert job.status == JobStatus.COMPLETED
    assert ocr.calls == 1
    manifest = json.loads(storage.get_bytes(job.artifacts["manifest"]))
    assert manifest["inference"]["ocr"]["reused_for_retry"] is True
