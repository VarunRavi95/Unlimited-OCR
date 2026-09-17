from __future__ import annotations

import gc
import gzip
import json
import logging
import queue
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

from app.config import Settings
from app.database import JobRepository
from app.schemas import JobStage, JobStatus
from app.services.bedrock import BedrockInvoiceClient, BedrockServiceError
from app.services.ocr import OCRResult, UnlimitedOCRClient
from app.services.storage import ObjectStorage
from app.services.trust import TrustAssessor
from app.trust_models import TrustAssessment

LOGGER = logging.getLogger(__name__)


def iso_now() -> str:
    return datetime.now(UTC).isoformat()


class JobProcessor:
    def __init__(self, *, settings: Settings, repository: JobRepository, storage: ObjectStorage, ocr_client: UnlimitedOCRClient, bedrock_client: BedrockInvoiceClient, trust_assessor: TrustAssessor | None = None):
        self.settings = settings
        self.repository = repository
        self.storage = storage
        self.ocr_client = ocr_client
        self.bedrock_client = bedrock_client
        self.trust_assessor = trust_assessor or TrustAssessor(settings)

    def process(self, job_id: str) -> None:
        job = self.repository.get(job_id)
        timings, artifacts = dict(job.timings), dict(job.artifacts)
        started = time.perf_counter()
        ocr_metadata: dict[str, object] = {}
        bedrock_metadata: dict[str, object] = {}
        trust_assessment: TrustAssessment | None = None
        trust_error: str | None = None
        persisted_ocr = self._load_persisted_ocr(job)
        active_stage = JobStage.BEDROCK if persisted_ocr else JobStage.OCR
        self.repository.update(
            job_id,
            status=JobStatus.PROCESSING,
            stage=active_stage,
            started_at=iso_now(),
            completed_at=None,
            error=None,
        )
        try:
            if persisted_ocr:
                ocr_result = persisted_ocr
                ocr_metadata = dict(ocr_result.metadata)
                ocr_metadata["reused_for_retry"] = True
            else:
                source = self.storage.get_bytes(job.source_key)
                stage_start = time.perf_counter()
                ocr_result = self._ocr_with_retry(source, job.content_type)
                timings["ocr_seconds"] = round(time.perf_counter() - stage_start, 3)
                ocr_metadata = dict(ocr_result.metadata)
                artifacts.update(self._persist_ocr(job_id, ocr_result))
            self.repository.update(job_id, stage=JobStage.BEDROCK, timings_json=timings, artifacts_json=artifacts)

            active_stage = JobStage.BEDROCK
            stage_start = time.perf_counter()
            bedrock_result = self.bedrock_client.extract(ocr_result.clean_markdown)
            result = bedrock_result.invoice
            bedrock_metadata = dict(bedrock_result.metadata)
            for warning in reversed(job.validation_warnings):
                if warning not in result.warnings:
                    result.warnings.insert(0, warning)
            timings["bedrock_seconds"] = round(time.perf_counter() - stage_start, 3)
            try:
                trust_assessment = self.trust_assessor.assess(
                    result,
                    clean_markdown=ocr_result.clean_markdown,
                    likelihood_data=ocr_result.likelihood_data,
                    validation_warnings=job.validation_warnings,
                    ocr_metadata=ocr_metadata,
                    page_count=job.page_count,
                )
            except Exception as error:
                trust_error = str(error)
                LOGGER.exception("Could not assess trust for job %s", job_id)
                result.warnings.append(
                    "Trust assessment was unavailable because its deterministic checks failed."
                )
            active_stage = JobStage.PERSISTING
            self.repository.update(job_id, stage=JobStage.PERSISTING)

            result_key = f"results/{job_id}/bedrock/result.json"
            self.storage.put_bytes(result_key, result.model_dump_json(indent=2).encode("utf-8"), "application/json")
            artifacts["result"] = result_key
            if trust_assessment is not None:
                trust_key = f"results/{job_id}/quality/trust-assessment.json"
                self.storage.put_bytes(
                    trust_key,
                    trust_assessment.model_dump_json(indent=2).encode("utf-8"),
                    "application/json",
                )
                artifacts["trust"] = trust_key
            timings["total_seconds"] = round(time.perf_counter() - started, 3)
            manifest_key = f"results/{job_id}/manifest.json"
            artifacts["manifest"] = manifest_key
            self.storage.put_bytes(manifest_key, json.dumps(self._manifest(job_id, JobStatus.COMPLETED, active_stage, timings, artifacts, ocr_metadata, bedrock_metadata, trust_assessment, trust_error, None), ensure_ascii=False, indent=2).encode("utf-8"), "application/json")
            self.repository.update(job_id, status=JobStatus.COMPLETED, stage=JobStage.COMPLETED, completed_at=iso_now(), timings_json=timings, artifacts_json=artifacts, result_json=result.model_dump(mode="json"), trust_json=trust_assessment.model_dump(mode="json") if trust_assessment else None)
        except Exception as error:
            LOGGER.exception("Job %s failed", job_id)
            if isinstance(error, BedrockServiceError):
                bedrock_metadata = dict(error.metadata)
            timings["total_seconds"] = round(time.perf_counter() - started, 3)
            manifest_key = f"results/{job_id}/manifest.json"
            artifacts["manifest"] = manifest_key
            try:
                manifest = self._manifest(job_id, JobStatus.FAILED, active_stage, timings, artifacts, ocr_metadata, bedrock_metadata, trust_assessment, trust_error, str(error))
                self.storage.put_bytes(manifest_key, json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"), "application/json")
            except Exception:
                LOGGER.exception("Could not persist failure manifest for %s", job_id)
            self.repository.update(job_id, status=JobStatus.FAILED, stage=JobStage.FAILED, completed_at=iso_now(), error=str(error), timings_json=timings, artifacts_json=artifacts)

    def _ocr_with_retry(self, source: bytes, content_type: str) -> OCRResult:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                return self.ocr_client.extract(source, content_type)
            except Exception as error:
                last_error = error
                if attempt == 0:
                    self.ocr_client.clear_temporary_state()
                    gc.collect()
                    time.sleep(1)
        assert last_error is not None
        raise last_error

    def _persist_ocr(self, job_id: str, result: OCRResult) -> dict[str, str]:
        raw_key = f"results/{job_id}/ocr/raw.txt"
        clean_key = f"results/{job_id}/ocr/clean.md"
        metadata_key = f"results/{job_id}/ocr/metadata.json"
        likelihood_key = f"results/{job_id}/ocr/token-likelihoods.json.gz"
        self.storage.put_bytes(raw_key, result.raw_text.encode("utf-8"), "text/plain")
        self.storage.put_bytes(clean_key, result.clean_markdown.encode("utf-8"), "text/markdown")
        self.storage.put_bytes(
            metadata_key,
            json.dumps(result.metadata, ensure_ascii=False, indent=2).encode("utf-8"),
            "application/json",
        )
        self.storage.put_bytes(
            likelihood_key,
            gzip.compress(
                json.dumps(result.likelihood_data, ensure_ascii=False).encode("utf-8")
            ),
            "application/gzip",
        )
        return {
            "raw": raw_key,
            "clean": clean_key,
            "ocr_metadata": metadata_key,
            "ocr_likelihoods": likelihood_key,
        }

    def _load_persisted_ocr(self, job) -> OCRResult | None:
        clean_key = job.artifacts.get("clean")
        if not clean_key:
            return None
        try:
            clean_markdown = self.storage.get_bytes(clean_key).decode("utf-8")
            metadata: dict[str, object] = {}
            likelihood_data: dict[str, object] = {
                "schema_version": 1,
                "available": False,
                "alignment_status": "historical_unavailable",
                "warning": "Token likelihoods were not captured for this historical OCR artifact.",
                "tokens": [],
                "clean_to_raw_spans": [],
            }
            metadata_key = job.artifacts.get("ocr_metadata")
            if metadata_key:
                metadata = json.loads(self.storage.get_bytes(metadata_key))
            likelihood_key = job.artifacts.get("ocr_likelihoods")
            if likelihood_key:
                likelihood_data = json.loads(
                    gzip.decompress(self.storage.get_bytes(likelihood_key))
                )
            return OCRResult(
                raw_text="",
                clean_markdown=clean_markdown,
                page_count=job.page_count,
                metadata=metadata,
                likelihood_data=likelihood_data,
            )
        except Exception:
            LOGGER.warning(
                "Stored OCR artifacts for job %s are unavailable; rerunning OCR",
                job.id,
                exc_info=True,
            )
            return None

    def _manifest(self, job_id: str, status: JobStatus, stage: JobStage, timings: dict[str, float], artifacts: dict[str, str], ocr_metadata: dict[str, object], bedrock_metadata: dict[str, object], trust_assessment: TrustAssessment | None, trust_error: str | None, error: str | None) -> dict[str, object]:
        job = self.repository.get(job_id)
        return {
            "job_id": job_id,
            "status": status.value,
            "stage": stage.value,
            "source": {"filename": job.filename, "content_type": job.content_type, "page_count": job.page_count, "s3_key": job.source_key, "validation_warnings": job.validation_warnings},
            "models": {"ocr": self.settings.ocr_model_id, "ocr_revision": self.settings.ocr_model_revision, "bedrock": self.settings.bedrock_model_id},
            "inference": {"ocr": ocr_metadata, "bedrock": bedrock_metadata},
            "trust": {
                "mode": self.settings.trust_mode,
                "ruleset_version": self.settings.trust_ruleset_version,
                "available": trust_assessment is not None,
                "document_status": trust_assessment.document.status.value if trust_assessment else None,
                "calibration_version": trust_assessment.calibration_version if trust_assessment else None,
                "error": trust_error,
            },
            "timings_seconds": timings,
            "artifacts": artifacts,
            "error": error,
            "generated_at": iso_now(),
        }


class JobWorker:
    def __init__(self, repository: JobRepository, process: Callable[[str], None]):
        self.repository, self.process = repository, process
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._queued: set[str] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="invoice-worker", daemon=True)
        self._thread.start()
        for job in self.repository.list_by_status(JobStatus.QUEUED):
            self.enqueue(job.id)

    def enqueue(self, job_id: str) -> None:
        with self._lock:
            if job_id in self._queued:
                return
            self._queued.add(job_id)
        self._queue.put(job_id)

    def stop(self) -> None:
        self._queue.put(None)
        if self._thread:
            self._thread.join(timeout=10)

    def _run(self) -> None:
        while True:
            job_id = self._queue.get()
            if job_id is None:
                self._queue.task_done()
                return
            try:
                self.process(job_id)
            finally:
                with self._lock:
                    self._queued.discard(job_id)
                self._queue.task_done()
