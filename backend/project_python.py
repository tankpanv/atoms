"""One private Python interpreter and install target per project workspace."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import venv


def owned_directory(root: Path, relative: str, uid: int) -> Path:
    path = root / relative
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f'Python 环境目录不得使用符号链接或越界路径：{relative}')
    path.mkdir(parents=True, exist_ok=True)
    if path.stat().st_uid != uid:
        # Repair legacy root-owned installs, including their nested files. Never
        # follow dependency symlinks into the image or another directory.
        own_tree(path, uid)
    return path


def own_tree(path: Path, uid: int):
    for directory, folders, files in os.walk(path, followlinks=False):
        for item in [Path(directory), *(Path(directory) / name for name in folders + files)]:
            if item.lstat().st_uid != uid or item.lstat().st_gid != uid:
                os.chown(item, uid, uid, follow_symlinks=False)


def environment(root: Path, uid: int) -> dict[str, str]:
    """Keep platform/user site-packages out of project Python and pip resolution.

    .python-packages remains the persistent target for existing workspaces and
    image caches. The private venv supplies Python/pip without system packages.
    """
    package_dir = owned_directory(root, '.python-packages', uid)
    cache = owned_directory(root, '.python-cache', uid)
    temporary = owned_directory(root, '.python-cache/tmp', uid)
    virtualenv = owned_directory(root, '.venv', uid)
    marker = virtualenv / '.atoms-interpreter'
    version = f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}'
    configuration = virtualenv / 'pyvenv.cfg'
    ready = (marker.is_file() and marker.read_text() == version
             and (virtualenv / 'bin/python').exists()
             and configuration.is_file()
             and 'include-system-site-packages = false' in configuration.read_text())
    if not ready:
        venv.EnvBuilder(with_pip=True, system_site_packages=False, symlinks=True).create(virtualenv)
        marker.write_text(version)
        own_tree(virtualenv, uid)
    return {
        'VIRTUAL_ENV': str(virtualenv),
        'PATH': f"{virtualenv / 'bin'}:{package_dir / 'bin'}:{root / 'node_modules/.bin'}:/usr/local/bin:/usr/bin:/bin",
        'PYTHONPATH': f'{package_dir}:{root}',
        'PYTHONNOUSERSITE': '1',
        'PIP_REQUIRE_VIRTUALENV': 'true',
        'PIP_TARGET': str(package_dir),
        'PIP_CACHE_DIR': str(cache / 'pip'),
        # Ignore user/global pip configuration that could redirect an install.
        'PIP_CONFIG_FILE': os.devnull,
        # Worker /tmp is intentionally noexec; build hooks/native imports need
        # an executable, project-private staging directory on the workspace.
        'TMPDIR': str(temporary),
    }
