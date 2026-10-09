"""Consistent file/SQLite snapshots for the project's private .atoms-data tree."""
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from project_snapshots import store_directory, save_blob, blob_path, file_identity


def data_root(root):
    path=Path(root)/'.atoms-data'
    if path.is_symlink() or not path.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError('项目私有数据路径无效')
    return path


def checked_path(root, name):
    if not isinstance(name,str) or not name or '\\' in name or any(part in ('','.','..') for part in name.split('/')) or name.startswith('/'):
        raise ValueError('私有数据快照路径无效')
    target=root/name
    if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
        raise ValueError('私有数据快照路径无效')
    return target


def capture_private_data(root):
    directory=data_root(root)
    files={}
    if not directory.exists():
        return files
    sqlite_files=set()
    for path in directory.rglob('*'):
        if path.is_symlink():
            raise ValueError('私有数据包含符号链接，不能完整保存')
        if path.is_file():
            with path.open('rb') as source:
                if source.read(16)==b'SQLite format 3\0':
                    sqlite_files.add(path)
    for path in sorted(directory.rglob('*')):
        if not path.is_file() or any(path==Path(str(db)+suffix) for db in sqlite_files for suffix in ('-wal','-shm','-journal')):
            continue
        name=str(path.relative_to(directory))
        if path in sqlite_files:
            fd,temporary=tempfile.mkstemp(dir=store_directory(root)); os.close(fd)
            try:
                with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as source, sqlite3.connect(temporary) as target:
                    source.backup(target)
                value=save_blob(root,Path(temporary))
            finally:
                Path(temporary).unlink(missing_ok=True)
        else:
            value=save_blob(root,path)
        files[name]={'object':value,'mode':path.stat().st_mode & 0o777}
    return files


def validate_private_data(root, files):
    if not isinstance(files,dict):
        raise ValueError('私有数据快照无效')
    directory=data_root(root)
    for name,item in files.items():
        checked_path(directory,name)
        if not isinstance(item.get('mode'),int) or not 0<=item['mode']<=0o777:
            raise ValueError('私有数据权限无效')
        if file_identity(blob_path(root,item['object']))!=item['object']:
            raise ValueError('私有数据快照对象校验失败')


def apply_private_data(root, files):
    validate_private_data(root,files)
    directory=data_root(root)
    staged=Path(tempfile.mkdtemp(prefix='private-data-',dir=store_directory(root)))
    try:
        for name,item in files.items():
            target=checked_path(staged,name)
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(blob_path(root,item['object']),target)
            target.chmod(item['mode'])
        if directory.exists():
            shutil.rmtree(directory)
        staged.replace(directory)
        from agent import project_uid
        from project_clone import own_copy
        try:
            uid=project_uid(__import__('uuid').UUID(Path(root).name))
        except ValueError:
            uid=os.geteuid()  # Standalone filesystem tests have no project UUID.
        own_copy(directory,uid)
    finally:
        if staged.exists():
            shutil.rmtree(staged)
