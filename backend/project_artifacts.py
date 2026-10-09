"""Downloadable generated documents, separate from editable source files."""
import os
from pathlib import Path

EXTENSIONS = {'.pptx', '.ppt', '.pdf', '.docx', '.doc', '.xlsx', '.xls', '.csv',
              '.md', '.html', '.txt', '.png', '.jpg', '.jpeg', '.webp', '.svg', '.zip'}


def artifacts(root: Path) -> list[dict]:
    output = root/'dist'
    if output.is_symlink() or not output.is_dir():
        return []
    entries=[]
    for directory,folders,files in os.walk(output):
        folders[:]=sorted(name for name in folders if not name.startswith('.') and not (Path(directory)/name).is_symlink())
        for name in sorted(files):
            path=Path(directory)/name
            if name.startswith('.') or path.suffix.lower() not in EXTENSIONS or path.is_symlink() or not path.is_file():
                continue
            if not path.resolve().is_relative_to(output.resolve()) or path.stat().st_size == 0:
                continue
            entries.append({'path':str(path.relative_to(root)), 'name':name, 'size':path.stat().st_size})
            if len(entries)>=100:
                return entries
    return entries


def artifact_file(root: Path, name: str) -> Path:
    if name not in {entry['path'] for entry in artifacts(root)}:
        raise ValueError('成果文件不存在或不可下载')
    target = (root/name).resolve()
    if not target.is_relative_to(root.resolve()/'dist'):
        raise ValueError('成果文件路径无效')
    return target
