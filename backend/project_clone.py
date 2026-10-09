"""Copy business source without crossing project boundaries or copying credentials."""
import os
import shutil
from pathlib import Path

SKIP = {'published', '.python-packages', '.python-cache', '.atoms-snapshots', '.runtime-python-deps', '.build-tmp', '.local', '.atoms-attachments', '.atoms-runtime', '.agent-session', '.ruff_cache', '.mypy_cache', 'node_modules', 'dist', '.git', '.venv', 'venv', '__pycache__', '.cache', '.npm-cache', '.atoms-data', '.auth_private.pem', '.pytest_cache', '.atoms', '.agent', '.attachments', '.runtime', 'coverage', 'env.connector'}

def copy_source(source, destination, include_build=False):
    source, destination = Path(source), Path(destination)
    total = 0
    files = 0
    for directory, folders, names in os.walk(source, followlinks=False):
        folders[:] = [name for name in folders if (name not in SKIP or (include_build and name == 'dist')) and not (Path(directory)/name).is_symlink()]
        for name in names:
            path = Path(directory)/name
            if path.is_symlink() or not path.is_file() or name in SKIP or name.startswith(('.env', '.atoms-upload-')):
                continue
            total += path.stat().st_size
            files += 1
            target = destination / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    return {'files': files, 'bytes': total}


def unpack_docker_stream(data):
    output = bytearray()
    errors = bytearray()
    while data:
        if len(data) < 8:
            raise ValueError('数据库导出响应不完整')
        channel, size = data[0], int.from_bytes(data[4:8], 'big')
        if size > len(data)-8:
            raise ValueError('数据库导出响应不完整')
        (errors if channel == 2 else output).extend(data[8:8+size])
        data = data[8+size:]
    return bytes(output), bytes(errors)


def own_copy(root, uid):
    # Copied build directories must belong to the destination worker too.
    # The generic workspace helper intentionally skips artifacts/caches.
    for directory, folders, files in os.walk(root, followlinks=False):
        for path in [Path(directory), *(Path(directory)/name for name in folders+files)]:
            if not path.is_symlink():
                os.chown(path,uid,uid,follow_symlinks=False)
