from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.schemas import JobStage, JobStatus
from app.services.storage import LocalStorage


def test_upload_status_and_source_artifact(tmp_path, png_bytes: bytes) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path, storage_backend="local", worker_enabled=False)
    app = create_app(settings=settings, storage=LocalStorage(tmp_path / "objects"))
    with TestClient(app) as client:
        response = client.post("/api/jobs", files={"file": ("invoice.png", png_bytes, "image/png")})
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        status = client.get(f"/api/jobs/{job_id}")
        assert status.status_code == 200
        assert status.json()["status"] == "queued"
        source = client.get(f"/api/jobs/{job_id}/artifacts/source")
        assert source.status_code == 200
        assert source.content == png_bytes
        assert client.post(f"/api/jobs/{job_id}/retry").status_code == 409


def test_rejects_bad_upload(tmp_path) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path, storage_backend="local", worker_enabled=False)
    app = create_app(settings=settings, storage=LocalStorage(tmp_path / "objects"))
    with TestClient(app) as client:
        response = client.post("/api/jobs", files={"file": ("invoice.txt", b"hello", "text/plain")})
        assert response.status_code == 400
        assert "Unsupported" in response.json()["detail"]


def test_unknown_job_is_404(tmp_path) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path, storage_backend="local", worker_enabled=False)
    app = create_app(settings=settings, storage=LocalStorage(tmp_path / "objects"))
    with TestClient(app) as client:
        assert client.get("/api/jobs/not-a-uuid").status_code == 404


def test_retry_preserves_successful_ocr_and_resumes_at_bedrock(
    tmp_path, png_bytes: bytes
) -> None:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        storage_backend="local",
        worker_enabled=False,
    )
    storage = LocalStorage(tmp_path / "objects")
    app = create_app(settings=settings, storage=storage)
    with TestClient(app) as client:
        created = client.post(
            "/api/jobs",
            files={"file": ("invoice.png", png_bytes, "image/png")},
        ).json()
        job_id = created["job_id"]
        clean_key = f"results/{job_id}/ocr/clean.md"
        metadata_key = f"results/{job_id}/ocr/metadata.json"
        likelihood_key = f"results/{job_id}/ocr/token-likelihoods.json.gz"
        storage.put_bytes(clean_key, b"# Invoice INV-100", "text/markdown")
        storage.put_bytes(metadata_key, b'{"finish_reason":"stop"}', "application/json")
        app.state.repository.update(
            job_id,
            status=JobStatus.FAILED,
            stage=JobStage.FAILED,
            error="Bedrock returned no text content",
            timings_json={"ocr_seconds": 12.5, "total_seconds": 15.0},
            artifacts_json={
                "raw": f"results/{job_id}/ocr/raw.txt",
                "clean": clean_key,
                "ocr_metadata": metadata_key,
                "ocr_likelihoods": likelihood_key,
                "manifest": f"results/{job_id}/manifest.json",
            },
        )

        response = client.post(f"/api/jobs/{job_id}/retry")
        assert response.status_code == 202
        retried = response.json()
        assert retried["status"] == "queued"
        assert retried["stage"] == "bedrock"
        assert retried["timings"] == {"ocr_seconds": 12.5}
        assert set(retried["artifacts"]) == {
            "raw",
            "clean",
            "ocr_metadata",
            "ocr_likelihoods",
        }
        assert retried["trust"] is None
