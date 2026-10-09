"""Strict, retryable cleanup of resources belonging to one project."""
from pathlib import Path
import shutil
import uuid
from published_storage import BUCKET, client


def delete_project_storage(project_id: uuid.UUID, records: list[dict], bucket: str = ''):
    # Include earlier builds and interrupted uploads, not only the last DB manifest.
    prefix = f'projects/{project_id}/'
    buckets = {BUCKET, *(item['bucket'] for item in records)}
    if bucket:
        buckets.add(bucket)
    for item in records:
        if not item['object_key'].startswith(prefix):
            raise ValueError('项目存储路径不属于当前项目，已停止删除')
    s3 = client()
    for name in buckets:
        try:
            for page in s3.get_paginator('list_object_versions').paginate(Bucket=name, Prefix=prefix):
                objects = [{'Key': item['Key'], 'VersionId': item['VersionId']}
                           for item in page.get('Versions', []) + page.get('DeleteMarkers', [])]
                for offset in range(0, len(objects), 1000):
                    result = s3.delete_objects(Bucket=name, Delete={'Objects': objects[offset:offset+1000], 'Quiet': True})
                    if result.get('Errors'):
                        raise RuntimeError('部分项目存储对象删除失败，请重试')
            # MinIO can omit uploads when Prefix is supplied even though
            # list_parts finds them. Paginate the bucket and filter locally.
            for page in s3.get_paginator('list_multipart_uploads').paginate(Bucket=name):
                for upload in page.get('Uploads', []):
                    if not upload['Key'].startswith(prefix):
                        continue
                    s3.abort_multipart_upload(Bucket=name, Key=upload['Key'], UploadId=upload['UploadId'])
        except Exception as exc:
            if getattr(exc, 'response', {}).get('Error', {}).get('Code') != 'NoSuchBucket':
                raise


def delete_project_directories(root: Path, project_id: uuid.UUID, owner_id: uuid.UUID, recorded: Path):
    root = root.resolve()
    paths = {recorded, root / 'projects' / project_id.hex,
             root / 'users' / owner_id.hex / project_id.hex, root / project_id.hex}
    # Validate every path before deleting anything. Never follow a substituted parent.
    for path in paths:
        if path.name != project_id.hex or not path.parent.resolve().is_relative_to(root):
            raise ValueError('项目目录超出存储范围，已停止删除')
    for path in paths:
        if path.is_symlink():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)  # Permission/IO errors must reach the caller.
