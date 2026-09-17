from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import Settings, get_settings
from app.database import JobRepository
from app.schemas import JobCreated, JobRecord, JobStage, JobStatus
from app.services.bedrock import BedrockInvoiceClient
from app.services.ocr import UnlimitedOCRClient
from app.services.processor import JobProcessor, JobWorker
from app.services.storage import ObjectStorage, create_storage
from app.services.validation import DocumentValidationError, validate_document

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOGGER = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def create_app(
    settings: Settings | None = None,
    storage: ObjectStorage | None = None,
) -> FastAPI:
    app_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        repository = JobRepository(app_settings.database_path)
        repository.initialize()
        recovered = repository.recover_interrupted()
        if recovered:
            LOGGER.warning("Marked %s interrupted jobs as failed", recovered)
        object_storage = storage or create_storage(app_settings)
        worker: JobWorker | None = None
        if app_settings.worker_enabled:
            processor = JobProcessor(
                settings=app_settings,
                repository=repository,
                storage=object_storage,
                ocr_client=UnlimitedOCRClient(app_settings),
                bedrock_client=BedrockInvoiceClient(app_settings),
            )
            worker = JobWorker(repository, processor.process)
            worker.start()
        app.state.settings = app_settings
        app.state.repository = repository
        app.state.storage = object_storage
        app.state.worker = worker
        yield
        if worker:
            worker.stop()

    app = FastAPI(
        title=app_settings.app_name,
        version=app_settings.app_version,
        lifespan=lifespan,
    )
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "max_upload_mb": app_settings.max_upload_bytes // (1024 * 1024),
                "max_pages": app_settings.max_pages,
                "minimum_dpi": app_settings.minimum_benchmark_dpi,
            },
        )

    @app.get("/api/health")
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "worker_enabled": app_settings.worker_enabled,
            "ocr_model": app_settings.ocr_model_id,
            "bedrock_model": app_settings.bedrock_model_id,
        }

    @app.post("/api/jobs", response_model=JobCreated, status_code=202)
    async def create_job(request: Request, file: UploadFile = File(...)) -> JobCreated:
        data = await file.read(app_settings.max_upload_bytes + 1)
        filename = Path(file.filename or "invoice").name
        try:
            document = validate_document(
                data,
                filename,
                max_bytes=app_settings.max_upload_bytes,
                max_pages=app_settings.max_pages,
                minimum_dpi=app_settings.minimum_benchmark_dpi,
            )
        except DocumentValidationError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

        job_id = str(uuid.uuid4())
        source_key = f"incoming/{job_id}/source{document.extension}"
        object_storage: ObjectStorage = request.app.state.storage
        try:
            object_storage.put_bytes(source_key, data, document.content_type)
        except Exception as error:
            LOGGER.exception("Could not persist upload for job %s", job_id)
            raise HTTPException(status_code=502, detail="Could not persist the upload to storage.") from error

        repository: JobRepository = request.app.state.repository
        job = repository.create(
            job_id=job_id,
            filename=filename,
            content_type=document.content_type,
            source_key=source_key,
            page_count=document.page_count,
            validation_warnings=list(document.warnings),
        )
        worker: JobWorker | None = request.app.state.worker
        if worker:
            worker.enqueue(job_id)
        return JobCreated(job_id=job.id, status=job.status, created_at=job.created_at)

    @app.get("/api/jobs/{job_id}", response_model=JobRecord)
    async def get_job(request: Request, job_id: str) -> JobRecord:
        return _get_job_or_404(request.app.state.repository, job_id)

    @app.post("/api/jobs/{job_id}/retry", response_model=JobRecord, status_code=202)
    async def retry_job(request: Request, job_id: str) -> JobRecord:
        repository: JobRepository = request.app.state.repository
        job = _get_job_or_404(repository, job_id)
        if job.status != JobStatus.FAILED:
            raise HTTPException(status_code=409, detail="Only failed jobs can be retried.")
        reusable_artifact_names = {"raw", "clean", "ocr_metadata", "ocr_likelihoods"}
        reusable_artifacts = {
            name: key
            for name, key in job.artifacts.items()
            if name in reusable_artifact_names
        }
        can_resume_bedrock = "clean" in reusable_artifacts
        reusable_timings = (
            {"ocr_seconds": job.timings["ocr_seconds"]}
            if can_resume_bedrock and "ocr_seconds" in job.timings
            else {}
        )
        updated = repository.update(
            job_id,
            status=JobStatus.QUEUED,
            stage=JobStage.BEDROCK if can_resume_bedrock else JobStage.QUEUED,
            started_at=None,
            completed_at=None,
            error=None,
            retry_count=job.retry_count + 1,
            timings_json=reusable_timings,
            artifacts_json=reusable_artifacts,
            result_json=None,
            trust_json=None,
        )
        worker: JobWorker | None = request.app.state.worker
        if worker:
            worker.enqueue(job_id)
        return updated

    @app.get("/api/jobs/{job_id}/artifacts/{artifact_name}")
    async def get_artifact(
        request: Request, job_id: str, artifact_name: str
    ) -> Response:
        job = _get_job_or_404(request.app.state.repository, job_id)
        key = job.source_key if artifact_name == "source" else job.artifacts.get(artifact_name)
        if not key:
            raise HTTPException(status_code=404, detail="Artifact is not available.")
        storage_adapter: ObjectStorage = request.app.state.storage
        try:
            content = storage_adapter.get_bytes(key)
        except Exception as error:
            raise HTTPException(status_code=404, detail="Artifact is not available.") from error
        media_types = {
            "source": job.content_type,
            "raw": "text/plain; charset=utf-8",
            "clean": "text/markdown; charset=utf-8",
            "result": "application/json",
            "manifest": "application/json",
            "ocr_metadata": "application/json",
            "ocr_likelihoods": "application/gzip",
            "trust": "application/json",
        }
        headers = {}
        if artifact_name != "source":
            extension = {
                "raw": "txt",
                "clean": "md",
                "ocr_likelihoods": "json.gz",
            }.get(artifact_name, "json")
            headers["Content-Disposition"] = (
                f'attachment; filename="{job_id}-{artifact_name}.{extension}"'
            )
        return Response(content, media_type=media_types.get(artifact_name, "application/octet-stream"), headers=headers)

    return app


def _get_job_or_404(repository: JobRepository, job_id: str) -> JobRecord:
    try:
        uuid.UUID(job_id)
        return repository.get(job_id)
    except (ValueError, KeyError) as error:
        raise HTTPException(status_code=404, detail="Job not found.") from error


app = create_app()
