"""Versioned production images, independent containers and atomic activation."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import hmac
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import uuid
from urllib.parse import quote

import httpx
from fastapi import HTTPException
from psycopg.types.json import Jsonb

from published_storage import BUCKET, client as storage_client, ensure_bucket, upload_build
from project_secrets import environment as secret_environment
from release_artifacts import package_context, runtime_manifest

tasks = {}
runtime_locks = {}


def init_publication_db(conn):
    conn.execute('ALTER TABLE projects ADD COLUMN IF NOT EXISTS active_release_id UUID')
    conn.execute('''CREATE TABLE IF NOT EXISTS project_releases (
        id UUID PRIMARY KEY, project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        version INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'queued',
        phase TEXT NOT NULL DEFAULT '等待打包', error TEXT NOT NULL DEFAULT '',
        image_id TEXT NOT NULL DEFAULT '', image_key TEXT NOT NULL DEFAULT '',
        bucket TEXT NOT NULL DEFAULT '', prefix TEXT NOT NULL DEFAULT '',
        manifest JSONB NOT NULL DEFAULT '{}', objects JSONB NOT NULL DEFAULT '[]',
        settings JSONB NOT NULL DEFAULT '{}', artifact JSONB NOT NULL DEFAULT '{}',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), activated_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS project_releases_one_pending ON project_releases(project_id) WHERE status IN ('queued','packaging','building','starting')")
    conn.execute('ALTER TABLE project_releases ADD COLUMN IF NOT EXISTS cancel_requested BOOLEAN NOT NULL DEFAULT FALSE')
    conn.execute('''CREATE TABLE IF NOT EXISTS published_databases (
        project_id UUID PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
        initialized BOOLEAN NOT NULL DEFAULT FALSE)''')


def release_name(project_id, release_id):
    # Docker DNS labels are limited to 63 characters. Keep the full release UUID.
    return f'atoms-release-{project_id.hex[:8]}-{release_id.hex}'


def network_name(project_id):
    return f'atoms-published-{project_id.hex}'


def token(service, release_id):
    return hmac.new(service.SECRET.encode(), b'published-release:' + release_id.bytes, hashlib.sha256).hexdigest()


def row(service, release_id):
    with service.connection() as conn:
        return conn.execute('SELECT * FROM project_releases WHERE id=%s', (release_id,)).fetchone()


def describe(release):
    if not release:
        return None
    return {key: str(release[key]) if key in {'id', 'project_id', 'created_at', 'activated_at'} and release[key] else release[key]
            for key in ('id', 'project_id', 'version', 'status', 'phase', 'error', 'created_at', 'activated_at')}


def phase(service, release_id, status, text):
    with service.connection() as conn:
        changed = conn.execute('UPDATE project_releases SET status=%s,phase=%s,updated_at=NOW() WHERE id=%s AND cancel_requested=FALSE', (status, text, release_id))
        if not changed.rowcount:
            raise RuntimeError('发布已取消')


async def network(service, project_id):
    name = network_name(project_id)
    existing = await service.docker('GET', f'/networks/{name}')
    if existing.status_code == 404:
        await service.docker('POST', '/networks/create', {'Name': name, 'Driver': 'bridge', 'CheckDuplicate': True,
            'Labels': {'atoms.role': 'release-network', 'atoms.project': project_id.hex, 'atoms.compose': service.COMPOSE_PROJECT}})
    filters = quote(json.dumps({'label': [f'com.docker.compose.project={service.COMPOSE_PROJECT}', 'com.docker.compose.service=db']}))
    databases = (await service.docker('GET', f'/containers/json?filters={filters}')).json()
    if len(databases) != 1:
        raise RuntimeError('发布数据库服务不可用')
    members = (await service.docker('GET', f'/networks/{name}')).json().get('Containers', {})
    for container_id, alias in [(databases[0]['Id'], 'db'), (socket.gethostname(), 'agent-service')]:
        if not any(key.startswith(container_id) or container_id.startswith(key) for key in members):
            await service.docker('POST', f'/networks/{name}/connect', {'Container': container_id, 'EndpointConfig': {'Aliases': [alias]}})
    return name


async def host_storage(service):
    own = (await service.docker('GET', f'/containers/{socket.gethostname()}/json')).json()
    mount = next((m for m in own['Mounts'] if m['Destination'] == '/workspaces'), None)
    if not mount:
        raise RuntimeError('发布持久化存储不可用')
    return Path(mount['Source'])


async def file_chunks(path):
    with Path(path).open('rb') as source:
        while chunk := await asyncio.to_thread(source.read, 1024 * 1024):
            yield chunk


async def build_image(service, context, tag):
    transport = httpx.AsyncHTTPTransport(uds=service.DOCKER_SOCKET)
    async with httpx.AsyncClient(transport=transport, base_url='http://docker', timeout=None) as client:
        async with client.stream('POST', f'/build?t={quote(tag)}&rm=1&forcerm=1&pull=0',
                                 content=file_chunks(context), headers={'Content-Type': 'application/x-tar'}) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line:
                    continue
                message = json.loads(line)
                if message.get('error'):
                    raise RuntimeError('发布镜像构建失败：' + message['error'][:1000])
    image = await service.docker('GET', f'/images/{tag}/json')
    if image.status_code == 404:
        raise RuntimeError('发布镜像未生成')
    return image.json()['Id']


async def save_image(service, image_id, path):
    transport = httpx.AsyncHTTPTransport(uds=service.DOCKER_SOCKET)
    async with httpx.AsyncClient(transport=transport, base_url='http://docker', timeout=None) as client:
        async with client.stream('GET', f'/images/{image_id}/get') as response:
            response.raise_for_status()
            with gzip.open(path, 'wb', compresslevel=1) as output:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    await asyncio.to_thread(output.write, chunk)


async def restore_image(service, release):
    image = await service.docker('GET', f'/images/{release["image_id"]}/json')
    if image.status_code != 404:
        return
    if not release['image_key']:
        raise RuntimeError('发布镜像归档不存在')
    directory = Path('/workspaces/.atoms-releases') / release['project_id'].hex / release['id'].hex
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / 'image.restore.tar.gz'
    await asyncio.to_thread(storage_client().download_file, release['bucket'], release['image_key'], str(archive))
    try:
        checksum = await asyncio.to_thread(file_digest, archive)
        if checksum != release['artifact'].get('image_sha256'):
            raise RuntimeError('发布镜像归档校验失败')
        transport = httpx.AsyncHTTPTransport(uds=service.DOCKER_SOCKET)
        async with httpx.AsyncClient(transport=transport, base_url='http://docker', timeout=None) as client:
            async with client.stream('POST', '/images/load?quiet=1', content=file_chunks(archive),
                                     headers={'Content-Type': 'application/x-tar'}) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line and json.loads(line).get('error'):
                        raise RuntimeError('发布镜像恢复失败')
        if (await service.docker('GET', f'/images/{release["image_id"]}/json')).status_code == 404:
            raise RuntimeError('归档未恢复指定版本镜像')
    finally:
        archive.unlink(missing_ok=True)


def file_digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def seed_files(root, home, uid):
    data = home / '.atoms-data'
    if data.exists():
        return
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    pending = home / '.seed-data'
    shutil.rmtree(pending, ignore_errors=True)
    pending.mkdir(mode=0o700)
    original = root / '.atoms-data'
    if original.is_dir() and not original.is_symlink():
        for source in original.rglob('*'):
            if source.is_symlink():
                raise ValueError('发布持久化数据不能包含符号链接')
            relative = source.relative_to(original)
            if any(p.startswith('configuration-recovery-') or p in {'application-secrets.json', '.application-secrets.lock'} for p in relative.parts):
                continue
            target = pending / relative
            if source.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif source.is_file() and not source.name.endswith(('-wal', '-shm', '-journal')):
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open('rb') as check:
                    sqlite = check.read(16) == b'SQLite format 3\x00'
                if sqlite:
                    with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as src, sqlite3.connect(target) as dst:
                        src.backup(dst)
                else:
                    shutil.copy2(source, target)
    pending.rename(data)
    from project_python import own_tree
    own_tree(home, uid)


async def start_release(service, release):
    project_id, release_id = release['project_id'], release['id']
    async with runtime_locks.setdefault(release_id, asyncio.Lock()):
        name = release_name(project_id, release_id)
        current = await service.docker('GET', f'/containers/{name}/json')
        network_id = await network(service, project_id)
        if current.status_code == 404:
            await restore_image(service, release)
            host = await host_storage(service)
            home = Path('/workspaces/.atoms-releases') / project_id.hex / 'data'
            if not (home / '.atoms-data').is_dir():
                raise RuntimeError('发布持久化数据目录缺失，拒绝使用开发数据替代')
            uid = 100_000 + project_id.int % 1_000_000_000
            from published_database import credentials
            schema, _, _, url = credentials(project_id, service.DATABASE_URL, service.SECRET)
            env = {'RELEASE_ROOT': release['manifest']['workspace'], 'RELEASE_MANIFEST': '/release/manifest.json',
                   'RELEASE_TOKEN': token(service, release_id), 'APP_DATABASE_URL': url, 'APP_DATABASE_SCHEMA': schema,
                   'PYTHONDONTWRITEBYTECODE': '1', 'NODE_ENV': 'production'}
            env.update(await asyncio.to_thread(secret_environment, home, uid))
            await service.docker('POST', f'/containers/create?name={name}', {
                'Image': release['image_id'], 'Env': [f'{k}={v}' for k,v in env.items()],
                'Labels': {'atoms.role': 'published-release', 'atoms.project': project_id.hex,
                           'atoms.release': release_id.hex, 'atoms.compose': service.COMPOSE_PROJECT},
                'ExposedPorts': {'9000/tcp': {}},
                'Healthcheck': {'Test': ['CMD', 'python', '-c',
                    'import os,urllib.request; urllib.request.urlopen(urllib.request.Request("http://localhost:9000/_atoms/health",headers={"X-Release-Token":os.environ["RELEASE_TOKEN"]}),timeout=2)'],
                    'Interval': 10000000000, 'Timeout': 3000000000, 'Retries': 3, 'StartPeriod': 60000000000},
                'HostConfig': {'NetworkMode': network_id, 'Init': True,
                    'Binds': [f'{host}/.atoms-releases/{project_id.hex}/data/.atoms-data:{release["manifest"]["workspace"]}/.atoms-data:rw'],
                    'ReadonlyRootfs': True, 'Tmpfs': {'/tmp': 'rw,nosuid,size=536870912'},
                    'CapDrop': ['ALL'], 'SecurityOpt': ['no-new-privileges:true'],
                    'Memory': int(os.getenv('PUBLISHED_MEMORY_MB', '1536')) * 1024 * 1024,
                    'NanoCpus': int(float(os.getenv('PUBLISHED_CPUS', '2')) * 1e9), 'PidsLimit': 512,
                    'RestartPolicy': {'Name': 'unless-stopped'}}})
        await service.docker('POST', f'/containers/{name}/start')
        async with httpx.AsyncClient(timeout=2) as client:
            for _ in range(300):
                try:
                    response = await client.get(f'http://{name}:9000/_atoms/health', headers={'X-Release-Token': token(service, release_id)})
                    if response.status_code == 200 and response.json().get('release_id') == str(release_id):
                        return name
                except (httpx.HTTPError, ValueError):
                    pass
                await asyncio.sleep(.25)
        raise RuntimeError('发布容器未通过就绪检查，旧版本继续提供服务')


def activate(service, release):
    project_id = release['project_id']
    with service.connection() as conn:
        project = conn.execute('SELECT * FROM projects WHERE id=%s FOR UPDATE', (project_id,)).fetchone()
        if not project or project['status'] == 'deleting':
            raise RuntimeError('项目正在删除，停止发布')
        current = conn.execute('SELECT cancel_requested FROM project_releases WHERE id=%s FOR UPDATE', (release['id'],)).fetchone()
        if current['cancel_requested']:
            raise RuntimeError('发布已取消，候选版本不能上线')
        settings = release['settings']
        if project['visibility'] == 'private' and settings.get('visibility') != 'public':
            raise RuntimeError('项目已设为私有，停止公开发布')
        if project['active_release_id']:
            conn.execute("UPDATE project_releases SET status='retired',phase='已被新版本替换',updated_at=NOW() WHERE id=%s", (project['active_release_id'],))
        # Entry, assets and backend selector change in the same transaction.
        conn.execute('DELETE FROM published_objects WHERE project_id=%s', (project_id,))
        for item in release['objects']:
            conn.execute('''INSERT INTO published_objects(project_id,object_path,bucket,object_key,content_type,size_bytes,etag)
                VALUES(%s,%s,%s,%s,%s,%s,%s)''', (project_id,item['path'],item['bucket'],item['key'],item['content_type'],item['size_bytes'],item['etag']))
        conn.execute('''UPDATE projects SET published=TRUE,active_release_id=%s,
            published_storage_bucket=%s,published_storage_prefix=%s,
            title=COALESCE(%s,title),visibility=COALESCE(%s,visibility),remove_badge=COALESCE(%s,remove_badge),
            publish_slug=COALESCE(%s,publish_slug),updated_at=NOW() WHERE id=%s''',
            (release['id'],release['bucket'],release['prefix'],settings.get('title'),settings.get('visibility'),settings.get('remove_badge'),settings.get('publish_slug'),project_id))
        conn.execute("UPDATE project_releases SET status='active',phase='已上线，开发与发布环境独立',activated_at=NOW(),error='',updated_at=NOW() WHERE id=%s", (release['id'],))
        from referrals import publish_reward
        publish_reward(conn, {'id': project['owner_id']})


async def retire_containers(service, project_id, active_id):
    # Let requests already forwarded to the previous version finish.
    await asyncio.sleep(65)
    filters = quote(json.dumps({'label': ['atoms.role=published-release', f'atoms.project={project_id.hex}', f'atoms.compose={service.COMPOSE_PROJECT}']}))
    for item in (await service.docker('GET', f'/containers/json?all=1&filters={filters}')).json():
        with service.connection() as conn:
            active = conn.execute('SELECT active_release_id FROM projects WHERE id=%s', (project_id,)).fetchone()
        if active and item['Labels']['atoms.release'] != (active['active_release_id'].hex if active['active_release_id'] else ''):
            await service.docker('POST', f'/containers/{item["Id"]}/stop?t=10')


async def publish(service, release_id):
    release = row(service, release_id)
    project_id = release['project_id']
    directory = Path('/workspaces/.atoms-releases') / project_id.hex / release_id.hex
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    context = directory / 'context.tar'
    image_archive = directory / 'image.tar.gz'
    # Deletion and publication serialize; mutations only wait for the snapshot.
    with service.connection() as guard:
        try:
            if not guard.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,38)) AS locked', (str(project_id),)).fetchone()['locked']:
                raise RuntimeError('项目已有发布操作')
            guard.execute('SELECT pg_advisory_lock_shared(hashtextextended(%s,17))', (str(project_id),))
            guard.commit()
            phase(service, release_id, 'packaging', '正在固定代码、构建产物和已安装依赖')
            while not guard.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,19)) AS locked', (str(project_id),)).fetchone()['locked']:
                guard.commit()
                await asyncio.sleep(.1)
            guard.commit()
            try:
                record, relative = service.project_record(project_id)
                if record['status'] != 'ready' and release['settings'].get('_migration'):
                    raise RuntimeError('请等待当前构建或还原结束再发布')
                root = Path('/workspaces') / relative
                seed_root = root
                with service.connection() as conn:
                    info = conn.execute('SELECT dev_command FROM projects WHERE id=%s', (project_id,)).fetchone()
                base = (await service.docker('GET', f'/images/{service.IMAGE}/json')).json()['Id']
                uid = 100_000 + project_id.int % 1_000_000_000
                if not release['settings'].get('_migration'):
                    from release_versions import build_version
                    # The staging tree is detached before build; the development
                    # lock is needed only to copy immutable snapshot blobs.
                    def unlock_workspace():
                        guard.execute('SELECT pg_advisory_unlock(hashtextextended(%s,19))', (str(project_id),))
                        guard.commit()
                    root, version_state = await build_version(service, release, root, directory, base, on_materialized=unlock_workspace)
                    from execution_contract import workspace_commands
                    info['dev_command'] = version_state.get('dev_command') or workspace_commands(root).get('dev', '')
                manifest = runtime_manifest(root, project_id, release_id, info['dev_command'])
                published_dist = None
                if release['settings'].get('_migration'):
                    published_dist = directory / 'legacy-dist'
                    published_dist.mkdir(exist_ok=True)
                    with service.connection() as conn:
                        original = conn.execute('SELECT published_html,published_storage_bucket FROM projects WHERE id=%s', (project_id,)).fetchone()
                        previous = conn.execute('SELECT object_path,bucket,object_key FROM published_objects WHERE project_id=%s', (project_id,)).fetchall()
                    if previous:
                        for item in previous:
                            destination = (published_dist / item['object_path']).resolve()
                            if not destination.is_relative_to(published_dist.resolve()):
                                raise ValueError('旧发布资源路径无效')
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            await asyncio.to_thread(storage_client().download_file, item['bucket'], item['object_key'], str(destination))
                    elif (root / 'published/index.html').is_file():
                        await asyncio.to_thread(shutil.copytree, root / 'published', published_dist, dirs_exist_ok=True)
                    elif original['published_html']:
                        (published_dist / 'index.html').write_text(original['published_html'])
                    else:
                        raise RuntimeError('旧发布入口缺失，拒绝用当前开发页面替换')
                artifact = await asyncio.to_thread(package_context, root, context, manifest, base, uid, service.COMPOSE_PROJECT, published_dist)
                # Static resources come from the frozen context, never a later build.
                import tarfile
                dist = directory / 'dist'
                dist.mkdir(exist_ok=True)
                with tarfile.open(context) as archive:
                    for entry in archive:
                        if entry.name.startswith('workspace/dist/') and entry.isfile():
                            target = dist / entry.name.removeprefix('workspace/dist/')
                            target.parent.mkdir(parents=True, exist_ok=True)
                            with archive.extractfile(entry) as source, target.open('wb') as destination:
                                shutil.copyfileobj(source, destination)
                if not (dist / 'index.html').is_file() and not manifest['command']:
                    raise RuntimeError('发布产物缺少 dist/index.html 且未配置发布服务')
                home = Path('/workspaces/.atoms-releases') / project_id.hex / 'data'
                await asyncio.to_thread(seed_files, seed_root, home, uid)
                if release['settings'].get('_migration') and (root / '.atoms-data/application-secrets.json').is_file() and not (home / '.atoms-data/application-secrets.json').exists():
                    # Preserve existing sessions at the one-time migration boundary.
                    source = root / '.atoms-data/application-secrets.json'
                    if source.is_symlink():
                        raise ValueError('旧发布密钥文件路径无效')
                    destination = home / '.atoms-data/application-secrets.json'
                    shutil.copyfile(source, destination)
                    os.chmod(destination, 0o600)
                    os.chown(destination, uid, uid)
                with service.connection() as conn:
                    from published_database import provision, names
                    provision(conn, project_id, service.DATABASE_URL, service.SECRET)
                    initialized = conn.execute('SELECT initialized FROM published_databases WHERE project_id=%s', (project_id,)).fetchone()
                if not initialized or not initialized['initialized']:
                    from clone_database import clone_database
                    def initialized_in_transaction(conn):
                        conn.execute('INSERT INTO public.published_databases(project_id,initialized) VALUES(%s,TRUE) ON CONFLICT(project_id) DO UPDATE SET initialized=TRUE', (project_id,))
                    await clone_database(project_id, project_id, service.connection, service.docker,
                        service.COMPOSE_PROJECT, service.DATABASE_URL, True, target_names=names(project_id), finalize=initialized_in_transaction)
            finally:
                guard.execute('SELECT pg_advisory_unlock(hashtextextended(%s,19))', (str(project_id),))
                guard.commit()
            phase(service, release_id, 'building', '正在构建完整发布镜像')
            tag = f'atoms-published:{project_id.hex}-{release_id.hex}'
            image_id = await build_image(service, context, tag)
            phase(service, release_id, 'building', '正在归档完整镜像与网站资源')
            prefix = f'projects/{project_id}/releases/{release_id}'
            await asyncio.to_thread(ensure_bucket)
            await asyncio.to_thread(storage_client().upload_file, str(context), BUCKET, prefix + '/context.tar')
            await save_image(service, image_id, image_archive)
            artifact['image_sha256'] = await asyncio.to_thread(file_digest, image_archive)
            artifact['image_bytes'] = image_archive.stat().st_size
            image_key = prefix + '/image.tar.gz'
            await asyncio.to_thread(storage_client().upload_file, str(image_archive), BUCKET, image_key)
            objects = []
            if (dist / 'index.html').is_file():
                _, _, objects = await asyncio.to_thread(upload_build, str(project_id), dist, prefix + '/dist')
            with service.connection() as conn:
                conn.execute('''UPDATE project_releases SET image_id=%s,image_key=%s,bucket=%s,prefix=%s,manifest=%s,
                    objects=%s,artifact=%s,updated_at=NOW() WHERE id=%s''',
                    (image_id,image_key,BUCKET,prefix,Jsonb(manifest),Jsonb(objects),Jsonb(artifact),release_id))
            release = row(service, release_id)
            phase(service, release_id, 'starting', '正在启动独立发布容器并检查服务')
            await start_release(service, release)
            activate(service, release)
            asyncio.create_task(retire_containers(service, project_id, release_id))
        except BaseException as error:
            detail = '发布任务被中断，请重试；已发布版本保留' if isinstance(error, asyncio.CancelledError) else str(error)
            with service.connection() as conn:
                conn.execute("UPDATE project_releases SET status='failed',phase='发布失败，旧版本保留',error=%s,updated_at=NOW() WHERE id=%s AND status<>'active'", (detail[:1500], release_id))
            name = release_name(project_id, release_id)
            try:
                await service.docker('DELETE', f'/containers/{name}?force=1')
            except Exception:
                pass
            if isinstance(error, asyncio.CancelledError):
                raise
        finally:
            guard.rollback()
            guard.execute('SELECT pg_advisory_unlock_all()')
            context.unlink(missing_ok=True)
            image_archive.unlink(missing_ok=True)
            shutil.rmtree(directory / 'dist', ignore_errors=True)
            shutil.rmtree(directory / 'legacy-dist', ignore_errors=True)
            shutil.rmtree(directory / 'build-workspaces', ignore_errors=True)


async def enqueue(service, project_id, settings):
    record, _ = service.project_record(project_id)
    if record['status'] in {'deleting', 'cloning'} or (settings.get('_migration') and record['status'] != 'ready'):
        raise HTTPException(409, '请等待当前构建或还原结束')
    with service.connection() as conn:
        selected = settings.get('publish_version')
        if selected is not None and (not isinstance(selected, int) or isinstance(selected, bool) or selected < 1):
            raise HTTPException(422, '发布版本必须为正整数')
        version_row = conn.execute('SELECT version FROM project_versions WHERE project_id=%s AND (%s::integer IS NULL OR version=%s) ORDER BY version DESC LIMIT 1', (project_id, selected, selected)).fetchone()
        if not version_row and not settings.get('_migration'):
            raise HTTPException(404 if selected is not None else 409, '所选构建版本不存在' if selected is not None else '暂无可发布的构建版本')
        pending = conn.execute("SELECT * FROM project_releases WHERE project_id=%s AND status IN ('queued','packaging','building','starting')", (project_id,)).fetchone()
        if pending:
            if version_row and pending['version'] != version_row['version']:
                raise HTTPException(409, '另一个版本正在发布，请等待完成后重试')
            return describe(pending)
        if not conn.execute('SELECT pg_try_advisory_xact_lock(hashtextextended(%s,38)) AS locked', (str(project_id),)).fetchone()['locked']:
            raise HTTPException(409, '已有发布或回退操作正在进行')
        release_id = uuid.uuid4()
        version = version_row['version'] if version_row else 0
        release = conn.execute('INSERT INTO project_releases(id,project_id,version,settings) VALUES(%s,%s,%s,%s) RETURNING *',
                               (release_id,project_id,version,Jsonb(settings))).fetchone()
    task = asyncio.create_task(publish(service, release_id))
    tasks[project_id] = task
    task.add_done_callback(lambda t: tasks.pop(project_id, None) if tasks.get(project_id) is t else None)
    return describe(release)


async def active_release(service, project_id):
    with service.connection() as conn:
        release = conn.execute("SELECT r.* FROM projects p JOIN project_releases r ON r.id=p.active_release_id WHERE p.id=%s AND p.published=TRUE AND p.visibility='public'", (project_id,)).fetchone()
    if not release:
        raise HTTPException(503, '当前发布版本尚未完成独立部署，请更新发布')
    await start_release(service, release)
    return release


async def stop_publication(service, project_id, *, delete=False):
    pending = tasks.get(project_id)
    if pending:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
    with service.connection() as conn:
        conn.execute('SELECT 1 FROM projects WHERE id=%s FOR UPDATE', (project_id,))
        conn.execute("UPDATE project_releases SET cancel_requested=TRUE,status='failed',phase='已取消发布',error='发布已取消' WHERE project_id=%s AND status IN ('queued','packaging','building','starting')", (project_id,))
        conn.execute('UPDATE projects SET published=FALSE,active_release_id=NULL WHERE id=%s', (project_id,))
        conn.execute("UPDATE project_releases SET status='retired',phase='已停止公开访问',updated_at=NOW() WHERE project_id=%s AND status='active'", (project_id,))
    filters = quote(json.dumps({'label': ['atoms.role=published-release', f'atoms.project={project_id.hex}', f'atoms.compose={service.COMPOSE_PROJECT}']}))
    for item in (await service.docker('GET', f'/containers/json?all=1&filters={filters}')).json():
        if delete:
            await service.docker('DELETE', f'/containers/{item["Id"]}?force=1')
        else:
            await service.docker('POST', f'/containers/{item["Id"]}/stop?t=10')
    if delete:
        await service.remove_network(network_name(project_id))
        with service.connection() as conn:
            from published_database import remove
            remove(conn, project_id)
        path = Path('/workspaces/.atoms-releases') / project_id.hex
        if path.is_symlink():
            raise RuntimeError('发布存储路径异常，拒绝删除')
        if path.exists():
            await asyncio.to_thread(shutil.rmtree, path)
        filters = quote(json.dumps({'label': [f'atoms.project={project_id.hex}', 'atoms.role=published-image', f'atoms.compose={service.COMPOSE_PROJECT}']}))
        for image in (await service.docker('GET', f'/images/json?filters={filters}')).json():
            await service.docker('DELETE', f'/images/{image["Id"]}')


async def recover_publications(service):
    # A process restart never turns an unverified candidate into the active release.
    interrupted = []
    with service.connection() as conn:
        pending = conn.execute("SELECT id,project_id FROM project_releases WHERE status IN ('queued','packaging','building','starting')").fetchall()
        for item in pending:
            if conn.execute('SELECT pg_try_advisory_xact_lock(hashtextextended(%s,38)) AS locked', (str(item['project_id']),)).fetchone()['locked']:
                conn.execute("UPDATE project_releases SET status='failed',phase='任务被中断，旧版本保留',error='发布服务重启，请重试发布' WHERE id=%s", (item['id'],))
                interrupted.append(item)
        releases = conn.execute("SELECT r.* FROM projects p JOIN project_releases r ON r.id=p.active_release_id WHERE p.published=TRUE").fetchall()
    for item in interrupted:
        await service.docker('DELETE', f'/containers/atoms-release-build-{item["id"].hex}?force=1')
        directory = Path('/workspaces/.atoms-releases') / item['project_id'].hex / item['id'].hex / 'build-workspaces'
        await asyncio.to_thread(shutil.rmtree, directory, True)
    for release in releases:
        try:
            await start_release(service, release)
        except Exception as error:
            print(f'Release recovery failed for {release["id"]}: {type(error).__name__}', flush=True)


async def migrate_legacy_publications(service):
    # Keep published frontends byte-identical, prioritizing apps with an API.
    with service.connection() as conn:
        projects = conn.execute("SELECT id,workspace_path FROM projects WHERE published=TRUE AND active_release_id IS NULL AND status='ready' ORDER BY updated_at DESC").fetchall()
    projects.sort(key=lambda item: not (Path('/workspaces') / item['workspace_path'] / 'backend').is_dir())
    limit = asyncio.Semaphore(max(1, int(os.getenv('PUBLICATION_MIGRATION_CONCURRENCY', '3'))))
    async def migrate(item):
        async with limit:
            try:
                with service.connection() as conn:
                    current = conn.execute('SELECT published,active_release_id FROM projects WHERE id=%s', (item['id'],)).fetchone()
                if not current or not current['published'] or current['active_release_id']:
                    return
                await enqueue(service, item['id'], {'_migration': True})
                if item['id'] in tasks:
                    await tasks[item['id']]
            except Exception as error:
                print(f'Legacy release migration failed for {item["id"]}: {type(error).__name__}', flush=True)
    await asyncio.gather(*(migrate(item) for item in projects))
