"""Persistent project-owned application secrets, never provider credentials."""
import fcntl
import json
import os
from pathlib import Path
import secrets
import tempfile


LOCAL_SECRET_NAMES = frozenset({
    'AUTH_TOKEN_SECRET', 'AUTH_SECRET', 'JWT_SECRET', 'JWT_SECRET_KEY',
    'JWT_SIGNING_SECRET', 'SESSION_SECRET', 'SESSION_SECRET_KEY', 'SECRET_KEY',
    'APP_SECRET', 'APP_SECRET_KEY', 'FLASK_SECRET_KEY', 'COOKIE_SECRET',
    'CSRF_SECRET', 'CSRF_SECRET_KEY',
})


def environment(root: Path, uid: int) -> dict[str, str]:
    """Create once per workspace; shell/build and every service share the values.

    Do not inherit a platform secret or manufacture a third-party API key.
    .atoms-data is persistent and excluded from source, clone and export.
    """
    directory = root / '.atoms-data'
    if directory.is_symlink() or not directory.resolve().is_relative_to(root.resolve()):
        raise ValueError('项目私有配置目录不得使用符号链接或越界路径')
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.stat().st_uid != uid:
        os.chown(directory, uid, uid)
    path = directory / 'application-secrets.json'
    lock = directory / '.application-secrets.lock'
    with os.fdopen(os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600), 'r+') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        # Workers deliberately lack CAP_FOWNER. Once this file belongs to the
        # project UID, root cannot chmod it; already-private files need no chmod.
        if os.fstat(guard.fileno()).st_mode & 0o777 != 0o600:
            raise ValueError('项目私有配置锁权限无效；需修复为0600')
        os.fchown(guard.fileno(), uid, uid)
        if path.is_symlink():
            raise ValueError('项目密钥文件不得使用符号链接')
        try:
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW)) as source:
                saved = json.load(source)
                repair_mode = os.fstat(source.fileno()).st_mode & 0o777 != 0o600
                os.fchown(source.fileno(), uid, uid)
        except FileNotFoundError:
            saved = {}
            repair_mode = False
        except (ValueError, OSError) as exc:
            raise ValueError('项目私有配置无法读取；保留原文件，不自动轮换密钥') from exc
        if not isinstance(saved, dict) or any(
                name in saved and (not isinstance(saved[name], str) or len(saved[name]) < 32)
                for name in LOCAL_SECRET_NAMES):
            raise ValueError('项目私有配置无效；保留原文件，不自动轮换密钥')
        missing = LOCAL_SECRET_NAMES - saved.keys()
        if missing or repair_mode:
            saved.update({name: secrets.token_urlsafe(48) for name in missing})
            fd, temporary = tempfile.mkstemp(prefix='.application-secrets-', dir=directory)
            try:
                with os.fdopen(fd, 'w') as output:
                    os.fchmod(output.fileno(), 0o600)
                    os.fchown(output.fileno(), uid, uid)
                    json.dump(saved, output)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)
    return {name: saved[name] for name in LOCAL_SECRET_NAMES}
