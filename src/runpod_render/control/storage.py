"""Object storage (Cloudflare R2 in production, moto/MinIO locally) via the S3 API."""

from __future__ import annotations

from dataclasses import dataclass

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .settings import Settings


def output_prefix(project_id: str, job_id: str, attempt_id: str) -> str:
    return f"outputs/{project_id}/{job_id}/{attempt_id}"


def upload_key(project_id: str, upload_id: str) -> str:
    return f"uploads/{project_id}/{upload_id}"


@dataclass(frozen=True)
class ObjectInfo:
    size: int
    content_type: str | None


class Storage:
    def __init__(self, settings: Settings) -> None:
        self.bucket = settings.s3_bucket

        def client(endpoint: str | None):
            return boto3.client(
                "s3", endpoint_url=endpoint, region_name=settings.s3_region,
                aws_access_key_id=settings.s3_access_key_id,
                aws_secret_access_key=settings.s3_secret_access_key,
                config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                              retries={"max_attempts": 3, "mode": "standard"}))

        self.s3 = client(settings.s3_endpoint_url)
        self._presigner = (client(settings.s3_presign_endpoint_url)
                           if settings.s3_presign_endpoint_url else self.s3)

    def ensure_bucket(self) -> None:
        """For local stacks only; R2 buckets are created by the owner."""
        try:
            self.s3.head_bucket(Bucket=self.bucket)
        except ClientError:
            self.s3.create_bucket(Bucket=self.bucket)

    def presign_put(self, key: str, content_type: str, ttl_s: int) -> str:
        return self._presigner.generate_presigned_url(
            "put_object", Params={"Bucket": self.bucket, "Key": key, "ContentType": content_type},
            ExpiresIn=ttl_s)

    def presign_get(self, key: str, ttl_s: int) -> str:
        return self._presigner.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=ttl_s)

    def head(self, key: str) -> ObjectInfo | None:
        try:
            r = self.s3.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return ObjectInfo(size=int(r["ContentLength"]), content_type=r.get("ContentType"))

    def get_bytes(self, key: str) -> bytes | None:
        """Small objects only (manifests)."""
        try:
            r = self.s3.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return r["Body"].read()
