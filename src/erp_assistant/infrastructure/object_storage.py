from functools import lru_cache
from typing import Protocol, cast

import boto3
from botocore.exceptions import ClientError

from erp_assistant.core.config import get_settings


class ObjectStorage(Protocol):
    def put_bytes(self, key: str, content: bytes, content_type: str) -> None: ...

    def get_bytes(self, key: str) -> bytes: ...


class S3ObjectStorage:
    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
    ) -> None:
        self.bucket = bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )

    def _ensure_bucket(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code not in {"404", "NoSuchBucket", "NotFound"}:
                raise
            self.client.create_bucket(Bucket=self.bucket)

    def put_bytes(self, key: str, content: bytes, content_type: str) -> None:
        self._ensure_bucket()
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=content,
            ContentType=content_type,
        )

    def get_bytes(self, key: str) -> bytes:
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        return cast(bytes, response["Body"].read())


@lru_cache
def get_object_storage() -> S3ObjectStorage:
    settings = get_settings()
    return S3ObjectStorage(
        endpoint=settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        bucket=settings.minio_bucket,
    )
