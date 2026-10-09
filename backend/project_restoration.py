"""Version runtime checkpoints and recoverable, verified workspace restoration."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile

from psycopg.types.json import Jsonb

from project_snapshots import blob_path, file_identity, save_blob, store_directory

SESSION = '.atoms/.agent-session'
BACKUP = 'active-restore'
GENERATED_DIRECTORIES = {'node_modules', 'dist', '__pycache__', '.venv', 'venv', '.python-packages',
                         '.python-cache', '.runtime-python-deps', '.atoms-runtime', '.build-tmp',
                         '.pytest_cache', '.mypy_cache', '.ruff_cache', '.npm-cache'}


def normalize_legacy_version(files, state):
    """Old snapshots included caches now excluded from project source.

    Regenerate these from historical manifests without relaxing safe_file or
    accepting traversal, secrets or private business data as source files.
    """
    if not isinstance(files, dict) or not isinstance(state, dict):
        raise ValueError('版本文件或运行配置无效')
    def generated(name):
        if (not isinstance(name, str) or not name or name.startswith('/') or '\\' in name
                or any(part in {'', '.', '..'} for part in name.split('/'))):
            raise ValueError(f'版本文件路径无效：{name}')
        return any(part in GENERATED_DIRECTORIES for part in name.split('/'))
    source = {name: value for name, value in files.items() if not generated(name)}
    state = {**state, 'modes': {name: mode for name, mode in state.get('modes', {}).items() if not generated(name)}}
    return source, state


def capture_runtime_state(root, command=''):
    """SQLite backup includes committed WAL pages, unlike copying the DB file."""
    root = Path(root)
    from agent import list_files, safe_file
    state = {'dev_command': command, 'session': {},
             'modes': {name: stat.S_IMODE(safe_file(root, name).stat().st_mode) for name in list_files(root)}}
    from private_data_snapshots import capture_private_data
    state['private_data'] = capture_private_data(root)
    directory = root / SESSION
    if directory.is_symlink() or not directory.resolve().is_relative_to(root.resolve()):
        raise ValueError('会话存储路径无效')
    if not directory.exists():
        return state
    for path in directory.iterdir():
        if not path.is_file() or path.is_symlink() or path.name.endswith(('-wal', '-shm', '-journal')):
            continue
        if path.suffix == '.sqlite3':
            fd, name = tempfile.mkstemp(dir=store_directory(root))
            os.close(fd)
            try:
                with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2) as source, sqlite3.connect(name) as target:
                    source.backup(target)
                state['session'][path.name] = save_blob(root, Path(name))
            finally:
                Path(name).unlink(missing_ok=True)
        else:
            state['session'][path.name] = save_blob(root, path)
    return state


def validate_runtime_state(root, state):
    if not isinstance(state, dict) or not isinstance(state.get('dev_command', ''), str):
        raise ValueError('版本运行配置无效')
    from agent import safe_file
    from private_data_snapshots import validate_private_data
    validate_private_data(root, state.get('private_data', {}))
    for name, mode in state.get('modes', {}).items():
        safe_file(root, name)
        if not isinstance(mode, int) or mode < 0 or mode > 0o777:
            raise ValueError('版本文件权限无效')
    for name, value in state.get('session', {}).items():
        if not name or Path(name).name != name or name in {'.', '..'}:
            raise ValueError('版本会话路径无效')
        if file_identity(blob_path(root, value)) != value:
            raise ValueError('版本会话对象校验失败')


def apply_runtime_state(root, state):
    validate_runtime_state(root, state)
    from private_data_snapshots import apply_private_data
    if 'private_data' in state:
        apply_private_data(root, state['private_data'])
    directory = root / SESSION
    if directory.is_symlink() or not directory.resolve().is_relative_to(root.resolve()):
        raise ValueError('会话存储路径无效')
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True, mode=0o700)
    for name, value in state.get('session', {}).items():
        shutil.copyfile(blob_path(root, value), directory / name)
    from agent import safe_file
    for name, mode in state.get('modes', {}).items():
        path = safe_file(root, name)
        owner = path.stat().st_uid
        controller = os.geteuid()
        # Workers have CAP_CHOWN but intentionally no CAP_FOWNER. A controller
        # cannot chmod project-owned files even with CAP_DAC_OVERRIDE. Own the
        # file only for this operation and always put its original owner back.
        if owner != controller:
            os.chown(path, controller, -1)
        try:
            path.chmod(mode)
        finally:
            if owner != controller:
                os.chown(path, owner, -1)
    # Legacy versions did not save private checkpoints. Clearing them prevents
    # the failed/latest task's demo/type/evidence from being shown for old code.


def generated_paths(root):
    """Dependencies and build caches must be recreated from the restored lockfiles."""
    generated = {'node_modules', 'dist', '__pycache__', '.venv', 'venv', '.python-packages',
                 '.python-cache', '.runtime-python-deps', '.atoms-runtime', '.build-tmp', '.pytest_cache', '.mypy_cache', '.ruff_cache'}
    private = {'.atoms-snapshots', '.git', '.atoms-data', '.atoms-attachments', '.agent-session', 'published'}
    for directory, folders, _ in os.walk(root):
        for name in list(folders):
            path = Path(directory) / name
            if name in generated:
                yield path.relative_to(root)
                folders.remove(name)
            elif name in private or path.is_symlink():
                folders.remove(name)
    marker = root / '.npm-cache/dependency-state.json'
    if marker.is_file():
        yield marker.relative_to(root)


async def capture_version_state(project_id, root, command=''):
    from runtime import runtime_status, stop_runtime, start_runtime
    current = runtime_status(project_id) or {}
    if current.get('running'):
        await stop_runtime(project_id)
    from project_data_client import checkpoint
    try:
        state = capture_runtime_state(root, command)
        state['data'] = await checkpoint(project_id, 'capture')
        return state
    finally:
        if current.get('running'):
            await start_runtime(project_id,current.get('command') or command,restart=True)


class WorkspaceBackup:
    """Write the recovery manifest before moving a single generated directory."""
    def __init__(self, root, project, runtime_state=None):
        from agent import snapshot_files
        self.root = root
        self.directory = store_directory(root) / BACKUP
        if self.directory.exists():
            raise ValueError('存在尚未处理的还原检查点，请等待工作区恢复')
        self.state = {'files': snapshot_files(root),
                      'runtime_state': runtime_state if runtime_state is not None else capture_runtime_state(root, project.get('dev_command', '')),
                      'project_status': project['status'],
                      'paths': [str(path) for path in generated_paths(root)]}
        self.directory.mkdir()
        (self.directory / 'manifest.json').write_text(json.dumps(self.state))

    def detach(self):
        for name in self.state['paths']:
            source, target = self.root / name, self.directory / 'generated' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            source.rename(target)

    def rollback(self):
        from agent import restore_files
        # Remove new dependency/output directories before bringing back the
        # exact previous tree. An interrupted move is detected by its backup.
        for path in generated_paths(self.root):
            if str(path) in self.state['paths'] and not (self.directory / 'generated' / path).exists():
                continue  # Detach was interrupted before moving this original.
            target = self.root / path
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
        restore_files(self.root, self.state['files'])
        apply_runtime_state(self.root, self.state['runtime_state'])
        for name in self.state['paths']:
            source, target = self.directory / 'generated' / name, self.root / name
            if source.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                source.rename(target)
        from agent import snapshot_files
        from agent_harness import source_digest
        if source_digest(snapshot_files(self.root)) != source_digest(self.state['files']):
            raise ValueError('恢复操作前文件校验失败，检查点已保留')

    def close(self):
        shutil.rmtree(self.directory)


def recover_workspace(root):
    """Called at worker startup before accepting any requests or queued tasks."""
    directory = store_directory(root) / BACKUP
    if not directory.exists():
        return None
    if not (directory / 'manifest.json').is_file():
        shutil.rmtree(directory)  # No detach can happen before the manifest.
        return None
    backup = WorkspaceBackup.__new__(WorkspaceBackup)
    backup.root, backup.directory = root, directory
    backup.state = json.loads((directory / 'manifest.json').read_text())
    backup.rollback()
    backup.close()
    return backup.state


async def restore_version(project_id, version, progress=lambda phase: None):
    from agent import ensure_workspace, restore_files, run_build, preview_document, set_workspace_owner, project_uid
    from jobs import connection
    from runtime import start_runtime, stop_runtime
    from agent_checks import browser_check
    from agent_session import session_status
    with connection() as conn:
        row = conn.execute('SELECT files,preview_html,runtime_state FROM project_versions WHERE project_id=%s AND version=%s', (project_id, version)).fetchone()
        project = conn.execute('SELECT status,dev_command FROM projects WHERE id=%s', (project_id,)).fetchone()
    if not row:
        raise ValueError('版本不存在')
    root = ensure_workspace(project_id)
    state = row.get('runtime_state') or {}
    row['files'], state = normalize_legacy_version(row['files'], state)
    validate_runtime_state(root, state)
    from project_data_client import checkpoint
    if not isinstance(state.get('data'), dict) or state['data'].get('format') != 1:
        raise ValueError('此旧版本没有数据库和浏览器数据快照，无法完整还原；当前代码和数据未修改')
    await checkpoint(project_id, 'validate', state['data']['id'])
    from agent import safe_file
    import base64
    for name, content in row['files'].items():
        safe_file(root, name)
        if isinstance(content, str):
            if '\x00' in content:
                raise ValueError(f'版本文本包含 NUL: {name}')
        elif isinstance(content, dict) and content.get('encoding') == 'base64':
            base64.b64decode(content['content'], validate=True)
        elif isinstance(content, dict) and content.get('encoding') == 'blob':
            if file_identity(blob_path(root, content)) != content:
                raise ValueError(f'版本对象校验失败: {name}；当前源码未删除')
        else:
            raise ValueError(f'版本文件格式无效: {name}')
    with connection() as conn:
        operation = conn.execute('SELECT result FROM project_restores WHERE project_id=%s', (project_id,)).fetchone()
    if operation:
        project['status'] = operation['result'].get('previous_status', project['status'])
    progress('正在校验版本文件')
    await stop_runtime(project_id)
    progress('正在保存操作前数据库和浏览器数据检查点')
    previous_state = await capture_version_state(project_id, root, project.get('dev_command', ''))
    backup = WorkspaceBackup(root, project, previous_state)
    changed = False
    committed = False
    try:
        # restore_files stages and verifies all blobs before replacing source.
        # Stop processes first so they cannot mutate the restored workspace.
        await stop_runtime(project_id)
        changed = True
        backup.detach()
        restore_files(root, row['files'])
        apply_runtime_state(root, state)
        set_workspace_owner(root, project_uid(project_id))
        progress('正在恢复数据库和浏览器数据')
        await checkpoint(project_id, 'restore', state['data']['id'])
        progress('正在恢复依赖并重新构建')
        code, output = await run_build(project_id)
        if code:
            raise ValueError('还原后构建失败：' + output[-1500:])
        progress('正在校验数据库结构、数据和浏览器存储')
        await checkpoint(project_id, 'verify', state['data']['id'])
        progress('正在启动服务并验证预览')
        command = state.get('dev_command', '')
        runtime = await start_runtime(project_id, command, restart=True)
        kind = session_status(root).get('application_type', 'web')
        preview_result = {'mode': 'none', 'url': '', 'output': output[-2000:]}
        if runtime:
            code, report = await browser_check(project_id, root, {'path': '/', 'actions': [], 'startup_check': True, 'configured_command': command})
            if code:
                raise ValueError('还原后预览验证失败：' + report[-2000:])
            preview_result = {'mode': 'live', 'url': runtime.prefix + '/', 'output': runtime.output[-2000:]}
        elif kind in {'web', 'service'}:
            raise ValueError('还原版本未能启动可用的预览服务')
        await checkpoint(project_id, 'verify', state['data']['id'])
        # Never use HTML from a different build as a successful fallback.
        from agent import snapshot_files
        from agent_harness import source_digest
        if source_digest(snapshot_files(root)) != source_digest(row['files']):
            raise ValueError('还原后文件与版本快照不一致，未提交还原结果')
        for name, mode in state.get('modes', {}).items():
            if stat.S_IMODE(safe_file(root, name).stat().st_mode) != mode:
                raise ValueError(f'还原后文件权限与版本快照不一致：{name}')
        preview = preview_document(root)
        with connection() as conn:
            conn.execute('UPDATE projects SET preview_html=%s,dev_command=%s,status=\'ready\',updated_at=NOW() WHERE id=%s', (preview, command, project_id))
            next_version = conn.execute('SELECT COALESCE(MAX(version),0)+1 AS n FROM project_versions WHERE project_id=%s', (project_id,)).fetchone()['n']
            conn.execute('INSERT INTO project_versions(project_id,version,summary,files,preview_html,runtime_state) VALUES(%s,%s,%s,%s,%s,%s)',
                         (project_id, next_version, f'还原版本 {version}', Jsonb(row['files']), preview, Jsonb(state)))
            conn.execute('DELETE FROM runtime_routes WHERE project_id=%s', (project_id,))
            if runtime:
                conn.execute('INSERT INTO runtime_routes(token_hash,project_id) VALUES(%s,%s)', (hashlib.sha256(runtime.token.encode()).hexdigest(), project_id))
            conn.execute("UPDATE project_restores SET status='succeeded',phase='还原完成，预览验证通过',result=%s,updated_at=NOW() WHERE project_id=%s",
                         (Jsonb({'restored_version': next_version, 'restored_preview': preview_result}), project_id))
            from restoration_messages import update_message
            update_message(conn,project_id,status='succeeded',result={'restored_version':next_version})
        committed = True
        backup.close()
        return next_version
    except BaseException as exc:
        if committed:
            return next_version  # Cleanup can be retried at worker startup.
        if changed:
            await stop_runtime(project_id)
            progress('还原未通过验证，正在恢复操作前的工作区')
            try:
                backup.rollback()
                await checkpoint(project_id, 'restore', backup.state['runtime_state']['data']['id'])
                set_workspace_owner(root, project_uid(project_id))
            except Exception as rollback_error:
                raise ValueError(f'{exc}；恢复操作前工作区失败：{rollback_error}，检查点已保留') from exc
        backup.close()
        raise
