"""Materialize immutable build versions without restoring the development workspace."""
import base64
import os
from pathlib import Path
import shutil

from agent import safe_file
from project_restoration import normalize_legacy_version
from project_snapshots import blob_path, file_identity


def materialize_version(source, destination, version):
    files, state = normalize_legacy_version(version['files'], version.get('runtime_state') or {})
    if not files:
        raise ValueError('此版本没有可发布的源码快照')
    destination.mkdir(parents=True)
    for name, value in files.items():
        # Validate against both roots; read only historical blobs, never current source.
        safe_file(source, name)
        target = safe_file(destination, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(value, str):
            if '\x00' in value:
                raise ValueError(f'版本文本包含 NUL：{name}')
            target.write_text(value, encoding='utf-8')
        elif isinstance(value, dict) and value.get('encoding') == 'base64':
            target.write_bytes(base64.b64decode(value['content'], validate=True))
        elif isinstance(value, dict) and value.get('encoding') == 'blob':
            blob = blob_path(source, value)
            if file_identity(blob) != value:
                raise ValueError(f'版本对象校验失败：{name}')
            shutil.copyfile(blob, target)
        else:
            raise ValueError(f'版本文件格式无效：{name}')
        mode = state.get('modes', {}).get(name, 0o644)
        if not isinstance(mode, int) or not 0 <= mode <= 0o777:
            raise ValueError(f'版本文件权限无效：{name}')
        os.chmod(target, mode)
    return state


async def build_version(service, release, source, directory, base_image, on_materialized=lambda: None):
    """A disposable build container sees only this historical source tree."""
    from project_publication import host_storage, phase
    from project_python import own_tree
    import asyncio
    project_id, release_id = release['project_id'], release['id']
    with service.connection() as conn:
        version = conn.execute('SELECT files,runtime_state FROM project_versions WHERE project_id=%s AND version=%s',
                               (project_id, release['version'])).fetchone()
    if not version:
        raise ValueError('所选构建版本不存在')
    build_root = directory / 'build-workspaces'
    root = build_root / project_id.hex
    state = await asyncio.to_thread(materialize_version, source, root, version)
    on_materialized()
    # The project UID must traverse the bind-mounted parent during build commands.
    build_root.chmod(0o711)
    uid = 100_000 + project_id.int % 1_000_000_000
    await asyncio.to_thread(own_tree, root, uid)
    host = await host_storage(service)
    relative = build_root.relative_to('/workspaces')
    name = 'atoms-release-build-' + release_id.hex
    phase(service, release_id, 'packaging', f'正在独立构建版本 {release["version"]}')
    try:
        await service.docker('POST', f'/containers/create?name={name}', {
            'Image': base_image, 'Cmd': ['python', '/app/release_build.py'],
            'Env': [f'PROJECT_ID={project_id}', 'WORKSPACE_ROOT=/workspaces', 'PYTHONDONTWRITEBYTECODE=1'],
            'Labels': {'atoms.role': 'release-build', 'atoms.project': project_id.hex,
                       'atoms.release': release_id.hex, 'atoms.compose': service.COMPOSE_PROJECT},
            'HostConfig': {'Binds': [f'{host / relative}:/workspaces:rw'], 'NetworkMode': 'bridge', 'Init': True,
                'CapDrop': ['ALL'], 'CapAdd': ['CHOWN', 'SETUID', 'SETGID', 'DAC_OVERRIDE'],
                'SecurityOpt': ['no-new-privileges:true'], 'ReadonlyRootfs': True,
                'Tmpfs': {'/tmp': 'rw,nosuid,noexec,size=536870912'},
                'Memory': service.MEMORY_MB * 1024 * 1024, 'NanoCpus': int(service.CPUS * 1e9),
                'CpusetCpus': service.worker_cpu_set(project_id), 'PidsLimit': service.PIDS_LIMIT}})
        await service.docker('POST', f'/containers/{name}/start')
        while True:
            if service_row_cancelled(service, release_id):
                raise RuntimeError('发布已取消')
            info = (await service.docker('GET', f'/containers/{name}/json')).json()
            if not info['State']['Running']:
                if info['State'].get('ExitCode') != 0:
                    raise RuntimeError(f'版本 {release["version"]} 的独立构建失败，开发工作区与线上版本保持不变；请查看发布构建日志')
                break
            await asyncio.sleep(.5)
    finally:
        # Docker multiplex headers are harmless in the private diagnostic log.
        try:
            logs = await service.docker('GET', f'/containers/{name}/logs?stdout=1&stderr=1')
            (directory / 'version-build.log').write_bytes(logs.content)
        finally:
            await service.docker('DELETE', f'/containers/{name}?force=1')
    return root, state


def service_row_cancelled(service, release_id):
    with service.connection() as conn:
        row = conn.execute('SELECT cancel_requested FROM project_releases WHERE id=%s', (release_id,)).fetchone()
    return not row or row['cancel_requested']
