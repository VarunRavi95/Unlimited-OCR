from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Protocol

import boto3
from botocore.exceptions import ClientError

from app.config import Settings


class ObjectStorage(Protocol):
    def put_bytes(self, key: str, data: bytes, content_type: str) -> None: ...
    def get_bytes(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...


class S3Storage:
    def __init__(self, bucket: str, region: str):
        if not bucket:
            raise ValueError("S3_BUCKET is required when STORAGE_BACKEND=s3")
        self.bucket = bucket
        self.client = boto3.client("s3", region_name=region)

    def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type, ServerSideEncryption="AES256")

    def get_bytes(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in {"404", "NoSuchKey"}:
                return False
            raise


class LocalStorage:
    """Development-only filesystem adapter with the same key semantics as S3."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        normalized = PurePosixPath(key)
        if normalized.is_absolute() or ".." in normalized.parts:
            raise ValueError("Unsafe object key")
        path = (self.root / Path(*normalized.parts)).resolve()
        if self.root not in path.parents and path != self.root:
            raise ValueError("Unsafe object key")
        return path

    def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        del content_type
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()


def create_storage(settings: Settings) -> ObjectStorage:
    backend = settings.storage_backend.lower()
    if backend == "local":
        return LocalStorage(settings.local_storage_path)
    if backend == "s3":
        return S3Storage(settings.s3_bucket, settings.aws_region)
    raise ValueError(f"Unsupported STORAGE_BACKEND: {settings.storage_backend}")
