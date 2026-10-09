"""Project-local immutable blobs keep version metadata independent of file size."""
from __future__ import annotations

import base64
import codecs
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import zipfile

from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

DIRECTORY = '.atoms-snapshots'
CHUNK_BYTES = 1024 * 1024
INLINE_FILE_BYTES = 120_000
INLINE_TOTAL_BYTES = 1024 * 1024


class Snapshot(dict):
    def __init__(self, root):
        super().__init__()
        self.root = Path(root)


def store_directory(root):
    path = Path(root) / DIRECTORY
    if path.is_symlink() or not path.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError('版本对象目录不得越出项目工作区')
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def file_identity(path):
    """Hash and classify even multi-GB assets with bounded memory."""
    checksum = hashlib.sha256()
    decoder = codecs.getincrementaldecoder('utf-8')()
    text, size = True, 0
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(CHUNK_BYTES), b''):
            checksum.update(chunk)
            size += len(chunk)
            if text:
                try:
                    text = '\x00' not in decoder.decode(chunk)
                except UnicodeDecodeError:
                    text = False
    if text:
        try:
            decoder.decode(b'', final=True)
        except UnicodeDecodeError:
            text = False
    return {'encoding': 'blob', 'sha256': checksum.hexdigest(), 'size': size, 'text': text}


def save_blob(root, path):
    directory = store_directory(root)
    fd, temporary = tempfile.mkstemp(prefix='pending-', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as output, path.open('rb') as source:
            shutil.copyfileobj(source, output, CHUNK_BYTES)
        temporary = Path(temporary)
        identity = file_identity(temporary)
        destination = directory / identity['sha256']
        if destination.is_symlink():
            raise ValueError('版本对象不得使用符号链接')
        if destination.exists():
            if file_identity(destination) != identity:
                raise ValueError('已有版本对象校验失败；未覆盖历史版本')
        else:
            temporary.replace(destination)
        return identity
    finally:
        Path(temporary).unlink(missing_ok=True)


def blob_path(root, value):
    checksum = value.get('sha256', '')
    if not isinstance(checksum, str) or not re.fullmatch('[0-9a-f]{64}', checksum):
        raise ValueError('版本对象哈希无效')
    path = store_directory(root) / checksum
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'版本对象缺失：{checksum}；当前源码未删除')
    return path


def snapshot(root, names, safe_file):
    files = Snapshot(root)
    inline_bytes = 0
    index = store_directory(root) / 'index.sqlite3'
    if index.is_symlink():
        raise ValueError('版本索引不得使用符号链接')
    def fingerprint(path):
        stat = path.stat()
        return json.dumps([stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino])
    with sqlite3.connect(index, timeout=30) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS objects(path TEXT PRIMARY KEY, fingerprint TEXT, value TEXT, object_stat TEXT)')
        for name in names:
            path = safe_file(root, name)
            size = path.stat().st_size
            if size <= INLINE_FILE_BYTES and inline_bytes + size <= INLINE_TOTAL_BYTES:
                raw = path.read_bytes()
                try:
                    text = raw.decode('utf-8')
                    if '\x00' in text:
                        raise UnicodeError()
                    files[name] = text
                except UnicodeError:
                    files[name] = {'encoding': 'base64', 'content': base64.b64encode(raw).decode()}
                inline_bytes += len(raw)
            else:
                current = fingerprint(path)
                row = conn.execute('SELECT fingerprint,value,object_stat FROM objects WHERE path=?', (name,)).fetchone()
                value = json.loads(row[1]) if row else None
                if row and row[0] == current:
                    try:
                        if fingerprint(blob_path(root, value)) == row[2]:
                            files[name] = value
                            continue
                    except ValueError:
                        pass
                value = save_blob(root, path)
                files[name] = value
                conn.execute('INSERT OR REPLACE INTO objects VALUES(?,?,?,?)',
                             (name, current, json.dumps(value), fingerprint(blob_path(root, value))))
    return files


def text_content(files, name):
    value = files.get(name)
    if isinstance(value, str):
        return value
    if (isinstance(files, Snapshot) and isinstance(value, dict)
            and value.get('encoding') == 'blob' and value.get('text')):
        return blob_path(files.root, value).read_text(encoding='utf-8')
    return None


def text_page(path, offset, limit):
    identity = file_identity(path)
    if not identity['text']:
        raise ValueError('二进制文件不能作为文本读取')
    total, parts = 0, []
    with path.open(encoding='utf-8', newline='') as stream:
        for chunk in iter(lambda: stream.read(65536), ''):
            start, end = max(0, offset - total), min(len(chunk), offset + limit - total)
            if start < end:
                parts.append(chunk[start:end])
            total += len(chunk)
    return ''.join(parts), total, identity['sha256']


def restore(root, files, names, safe_file):
    """Validate and stage the ENTIRE version before deleting any current source."""
    directory = Path(tempfile.mkdtemp(prefix='restore-', dir=store_directory(root)))
    try:
        targets = {}
        for name, value in files.items():
            target = safe_file(root, name)
            stage = directory / name
            stage.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(value, str):
                stage.write_bytes(value.encode('utf-8'))
            elif isinstance(value, dict) and value.get('encoding') == 'base64':
                stage.write_bytes(base64.b64decode(value['content'], validate=True))
            elif isinstance(value, dict) and value.get('encoding') == 'blob':
                shutil.copyfile(blob_path(root, value), stage)
                if file_identity(stage) != value:
                    raise ValueError(f'版本对象校验失败：{name}；当前源码未删除')
            else:
                raise ValueError(f'版本文件格式无效：{name}')
            targets[name] = (target, stage)
        # Preflight parent conflicts too. Files scheduled for removal may become
        # directories in the restored version, and vice versa.
        obsolete = [safe_file(root, name) for name in names if name not in files]
        target_paths = {target for target, _ in targets.values()}
        remove_directories = set()
        for target, _ in targets.values():
            if any(parent in target_paths for parent in target.parents):
                raise ValueError(f'版本文件路径互相冲突：{target.name}')
            if any(parent.is_file() and parent not in obsolete for parent in target.parents):
                raise ValueError(f'版本恢复父路径不是目录：{target.name}')
            if target.is_dir():
                for child in target.rglob('*'):
                    if child.is_symlink() or (child.is_file() and child not in obsolete):
                        raise ValueError(f'恢复会覆盖未纳入版本的文件：{target.name}')
                    if child.is_dir():
                        remove_directories.add(child)
                remove_directories.add(target)
        for path in obsolete:
            path.unlink()
        for path in sorted(remove_directories, key=lambda path: len(path.parts), reverse=True):
            path.rmdir()
        for target, stage in targets.values():
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage, target)
    finally:
        shutil.rmtree(directory)


async def receive_upload(request, target):
    """Stream to disk; interrupted uploads never replace the previous file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.atoms-upload-', dir=target.parent)
    size = 0
    try:
        with os.fdopen(fd, 'wb') as output:
            async for chunk in request.stream():
                output.write(chunk)
                size += len(chunk)
        os.replace(temporary, target)
        return size
    finally:
        Path(temporary).unlink(missing_ok=True)


def archive_response(root, entries, filename):
    """ZIP64 on disk, then stream the response without duplicating assets in RAM."""
    fd, name = tempfile.mkstemp(prefix='archive-', suffix='.zip', dir=store_directory(root))
    os.close(fd)
    path = Path(name)
    try:
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            for source, relative in entries:
                archive.write(source, relative)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return FileResponse(path, media_type='application/zip', filename=filename,
                        background=BackgroundTask(path.unlink, missing_ok=True))
