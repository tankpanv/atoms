"""Disk-backed, complete release contexts; installed dependencies are included."""
import hashlib
import io
import json
from pathlib import Path
import stat
import tarfile

TRANSIENT = {'.git', '.atoms', '.atoms-snapshots', '.atoms-data', '.npm-cache',
             '.python-cache', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache'}


def runtime_manifest(root, project_id, release_id, dev_command=''):
    from runtime import configured_services
    from execution_contract import validate_command
    settings = json.loads((root / '.atoms-workspace.json').read_text()) if (root / '.atoms-workspace.json').exists() else {}
    publish = settings.get('publish', {})
    if isinstance(publish, str):
        publish = {'command': publish}
    if not isinstance(publish, dict):
        raise ValueError('publish 必须是命令字符串或对象')
    services = publish.get('services', configured_services(root))
    # Apply the same service validation to publication-specific overrides.
    if services != settings.get('services', []):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            location = Path(directory)
            (location / '.atoms-workspace.json').write_text(json.dumps({'services': services}))
            configured_services(location)
    api_service = publish.get('api_service') or next((s['name'] for s in services if s['name'] in {'api', 'backend'}), '')
    if not api_service and len(services) == 1:
        api_service = services[0]['name']
    if api_service and api_service not in {s['name'] for s in services}:
        raise ValueError('publish.api_service 不存在')
    package_file, frontend_file = root / 'package.json', root / 'frontend/package.json'
    package = json.loads(package_file.read_text()) if package_file.is_file() else {}
    frontend = json.loads(frontend_file.read_text()) if frontend_file.is_file() else {}
    scripts = package.get('scripts', {})
    vite = ('vite' in {**package.get('dependencies', {}), **package.get('devDependencies', {}),
                     **frontend.get('dependencies', {}), **frontend.get('devDependencies', {})}
            or (root / 'node_modules/vite/package.json').is_file()
            or 'vite' in scripts.get('dev', ''))
    frontend_dev = ('vite' in dev_command or (vite and ('run dev' in dev_command or 'run serve' in dev_command)))
    command = publish.get('command', '')
    # Vite and workspace wrappers serve the built dist. Explicit production
    # commands and custom all-in-one servers retain their executable contract.
    if not command and not api_service:
        if 'start' in scripts and 'vite' not in scripts['start']:
            command = 'npm run start'
        elif dev_command and not frontend_dev and 'npm run dev' not in dev_command:
            command = dev_command
        elif 'dev' in scripts and not vite:
            command = dev_command or 'npm run dev'
    if command:
        validate_command(command)
    ready_path = publish.get('ready_path', '/')
    if not isinstance(ready_path, str) or not ready_path.startswith('/') or ready_path.startswith('//'):
        raise ValueError('发布 ready_path 无效')
    return {'project_id': str(project_id), 'release_id': str(release_id), 'services': services,
            'api_service': api_service, 'command': command, 'ready_path': ready_path,
            'dynamic': bool(command), 'workspace': f'/workspaces/{project_id.hex}'}


def package_context(root, output, manifest, base_image, uid, compose_project="atoms-demo", dist_override=None):
    """Never materialize a whole project/archive in memory, or follow host links."""
    root = root.resolve()
    digest = hashlib.sha256()
    count = size = 0
    source = Path(__file__).parent
    dockerfile = (f'FROM {base_image}\n'
                  f'COPY --chown={uid}:{uid} workspace/ {manifest["workspace"]}/\n'
                  'COPY release_runner.py service_proxy.py /release/\n'
                  'COPY manifest.json /release/manifest.json\n'
                  f'LABEL atoms.role="published-image" atoms.project="{uuid_hex(manifest["project_id"])}" atoms.compose="{compose_project}"\n'
                  'WORKDIR /release\n'
                  f'USER {uid}:{uid}\n'
                  'CMD ["uvicorn", "release_runner:app", "--host", "0.0.0.0", "--port", "9000"]\n')
    with tarfile.open(output, 'w') as archive:
        for name, data in [('Dockerfile', dockerfile.encode()), ('manifest.json', json.dumps(manifest).encode()),
                           ('release_runner.py', (source / 'release_runner.py').read_bytes()),
                           ('service_proxy.py', (source / 'service_proxy.py').read_bytes())]:
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o644
            archive.addfile(info, io.BytesIO(data))
            digest.update(name.encode() + data)
        def add(directory, base=root, prefix='workspace'):
            nonlocal count, size
            for path in sorted(directory.iterdir()):
                if path.name in TRANSIENT or path.name in {'env.connector', '.env.connector', '.DS_Store'}:
                    continue
                if directory == root and path.name == 'dist' and dist_override:
                    continue
                relative = path.relative_to(base).as_posix()
                before = path.lstat()
                if path.is_symlink():
                    target = path.readlink()
                    resolved = path.resolve()
                    if not resolved.is_relative_to(root) and not str(resolved).startswith(('/usr/local/', '/usr/lib/', '/usr/bin/', '/lib/')):
                        raise ValueError(f'发布文件符号链接越界：{relative}')
                elif not stat.S_ISREG(before.st_mode) and not stat.S_ISDIR(before.st_mode):
                    raise ValueError(f'发布产物包含特殊文件：{relative}')
                info = archive.gettarinfo(str(path), prefix + '/' + relative)
                if info.isdir():
                    archive.addfile(info)
                    add(path, base, prefix)
                    continue
                if info.isfile():
                    with path.open('rb') as content:
                        archive.addfile(info, content)
                    after = path.stat()
                    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                        raise ValueError(f'打包期间项目文件发生变化：{relative}')
                    size += info.size
                else:
                    archive.addfile(info)
                count += 1
                digest.update(f'{relative}:{info.size}:{info.mtime}:{info.linkname}'.encode())
        add(root)
        if dist_override:
            add(Path(dist_override), Path(dist_override), 'workspace/dist')
    # The persisted artifact has its own byte-level integrity checksum.
    checksum = hashlib.sha256()
    with Path(output).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            checksum.update(chunk)
    return {'files': count, 'bytes': size, 'context_sha256': checksum.hexdigest()}


def uuid_hex(value):
    import uuid
    return uuid.UUID(value).hex
