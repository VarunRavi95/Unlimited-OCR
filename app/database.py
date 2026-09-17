from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.schemas import JobRecord, JobStage, JobStatus


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class JobRepository:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    source_key TEXT NOT NULL,
                    page_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    error TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    validation_warnings_json TEXT NOT NULL DEFAULT '[]',
                    timings_json TEXT NOT NULL DEFAULT '{}',
                    artifacts_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT,
                    trust_json TEXT
                )
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()}
            if "validation_warnings_json" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN validation_warnings_json TEXT NOT NULL DEFAULT '[]'")
            if "trust_json" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN trust_json TEXT")

    def create(
        self,
        *,
        job_id: str,
        filename: str,
        content_type: str,
        source_key: str,
        page_count: int,
        validation_warnings: list[str] | None = None,
    ) -> JobRecord:
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, status, stage, filename, content_type, source_key,
                    page_count, validation_warnings_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    JobStatus.QUEUED,
                    JobStage.QUEUED,
                    filename,
                    content_type,
                    source_key,
                    page_count,
                    json.dumps(validation_warnings or [], ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return self.get(job_id)

    def get(self, job_id: str) -> JobRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._row_to_record(row)

    def list_by_status(self, status: JobStatus) -> list[JobRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY created_at", (status,)
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def update(self, job_id: str, **changes: Any) -> JobRecord:
        if not changes:
            return self.get(job_id)
        allowed = {
            "status",
            "stage",
            "started_at",
            "completed_at",
            "error",
            "retry_count",
            "validation_warnings_json",
            "timings_json",
            "artifacts_json",
            "result_json",
            "trust_json",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"Unsupported job fields: {sorted(unknown)}")
        changes["updated_at"] = utc_now()
        assignments = ", ".join(f"{name} = ?" for name in changes)
        values = [self._serialize(value) for value in changes.values()]
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE jobs SET {assignments} WHERE id = ?", values + [job_id]
            )
            if cursor.rowcount != 1:
                raise KeyError(job_id)
        return self.get(job_id)

    def recover_interrupted(self) -> int:
        now = utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = ?, stage = ?, error = ?, completed_at = ?, updated_at = ?
                WHERE status = ?
                """,
                (
                    JobStatus.FAILED,
                    JobStage.FAILED,
                    "Processing was interrupted by an application restart. Retry the job.",
                    now,
                    now,
                    JobStatus.PROCESSING,
                ),
            )
        return cursor.rowcount

    @staticmethod
    def _serialize(value: Any) -> Any:
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, (JobStatus, JobStage)):
            return value.value
        if isinstance(value, datetime):
            return value.isoformat()
        return value

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            id=row["id"],
            status=row["status"],
            stage=row["stage"],
            filename=row["filename"],
            content_type=row["content_type"],
            source_key=row["source_key"],
            page_count=row["page_count"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            error=row["error"],
            retry_count=row["retry_count"],
            validation_warnings=json.loads(row["validation_warnings_json"] or "[]"),
            timings=json.loads(row["timings_json"] or "{}"),
            artifacts=json.loads(row["artifacts_json"] or "{}"),
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            trust=json.loads(row["trust_json"]) if row["trust_json"] else None,
        )
