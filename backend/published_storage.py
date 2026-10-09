"""MinIO storage for immutable published project builds."""

from __future__ import annotations

import mimetypes
import os
import uuid
from pathlib import Path

import boto3
from botocore.config import Config


BUCKET = os.getenv("MINIO_BUCKET", "atoms-published")
ENDPOINT = os.getenv("MINIO_ENDPOINT_URL", "http://minio:9000")

_client = None


def client():
    global _client
    if _client is None:
        _client = boto3.client(
            "s3",
            endpoint_url=ENDPOINT,
            aws_access_key_id=os.getenv("MINIO_ACCESS_KEY", "atoms-minio"),
            aws_secret_access_key=os.getenv("MINIO_SECRET_KEY", "atoms-minio-local-password"),
            region_name=os.getenv("MINIO_REGION", "us-east-1"),
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}, retries={"max_attempts": 5, "mode": "standard"}),
        )
    return _client


def ensure_bucket():
    s3 = client()
    try:
        s3.head_bucket(Bucket=BUCKET)
    except Exception as error:
        code = str(getattr(error, "response", {}).get("Error", {}).get("Code", ""))
        if code not in {"404", "NoSuchBucket", "NotFound"}:
            raise
        try:
            s3.create_bucket(Bucket=BUCKET)
        except Exception:
            # Another API worker may have created it between head and create.
            s3.head_bucket(Bucket=BUCKET)


def upload_build(project_id: str, dist: Path) -> tuple[str, str, list[dict]]:
    """Upload a complete dist tree and return bucket, prefix and object metadata."""
    ensure_bucket()
    root = dist.resolve()
    files = sorted(path for path in root.rglob("*") if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root))
    if not any(path.relative_to(root).as_posix() == "index.html" for path in files):
        raise ValueError("构建产物中缺少 index.html")

    prefix = f"projects/{project_id}/published/{uuid.uuid4().hex}"
    objects: list[dict] = []
    s3 = client()
    for path in files:
        relative = path.relative_to(root).as_posix()
        key = f"{prefix}/{relative}"
        content_type = mimetypes.guess_type(relative)[0] or "application/octet-stream"
        cache_control = "no-cache" if relative == "index.html" else "public, max-age=31536000, immutable"
        s3.upload_file(str(path), BUCKET, key, ExtraArgs={"ContentType": content_type, "CacheControl": cache_control})
        head = s3.head_object(Bucket=BUCKET, Key=key)
        objects.append({
            "path": relative,
            "bucket": BUCKET,
            "key": key,
            "content_type": head.get("ContentType", content_type),
            "size_bytes": head["ContentLength"],
            "etag": head.get("ETag", "").strip('"'),
        })
    return BUCKET, prefix, objects


def open_object(bucket: str, key: str):
    return client().get_object(Bucket=bucket, Key=key)


def delete_objects(bucket: str, keys: list[str]):
    if not keys:
        return
    s3 = client()
    for offset in range(0, len(keys), 1000):
        result = s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": key} for key in keys[offset:offset + 1000]], "Quiet": True})
        if result.get("Errors"):
            raise RuntimeError("部分存储对象删除失败")
