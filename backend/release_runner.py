"""Small release supervisor. No agent, workspace editor or platform DB credential."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hmac
import json
import os
from pathlib import Path
import signal

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse
from service_proxy import forward_http, forward_websocket

ROOT = Path(os.environ.get('RELEASE_ROOT', '/release/workspace'))
MANIFEST = json.loads(Path(os.environ.get('RELEASE_MANIFEST', '/release/manifest.json')).read_text())
TOKEN = os.environ['RELEASE_TOKEN']
processes = []
outputs = {}
api_target = ''
main_target = ''


async def capture(name, process):
    while chunk := await process.stdout.read(4096):
        outputs[name] = (outputs.get(name, '') + chunk.decode(errors='replace'))[-8000:]


async def launch(name, command, port, ready_path, environment):
    env = {**environment, 'PORT': str(port)}
    process = await asyncio.create_subprocess_exec('/bin/sh', '-c', command, cwd=ROOT, env=env,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, start_new_session=True)
    processes.append(process)
    asyncio.create_task(capture(name, process))
    async with httpx.AsyncClient(timeout=2) as client:
        for _ in range(240):
            if process.returncode is not None:
                break
            try:
                response = await client.get(f'http://127.0.0.1:{port}{ready_path}')
                if 200 <= response.status_code < 400:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(.25)
    # Output stays in the container logs, never in a public response.
    raise RuntimeError(f'Release service {name} did not become healthy: {outputs.get(name, "")[-2000:]}')


async def watch_services():
    while True:
        await asyncio.sleep(1)
        if any(process.returncode is not None for process in processes):
            # Docker restarts the complete frozen release, including all services.
            os.kill(os.getpid(), signal.SIGTERM)
            return


@asynccontextmanager
async def lifespan(app):
    global api_target, main_target
    env = dict(os.environ)
    # The coordinator credential belongs to the supervisor, not application code.
    for key in ('RELEASE_TOKEN', 'RELEASE_MANIFEST', 'RELEASE_ROOT'):
        env.pop(key, None)
    env.update(PATH=f'{ROOT}/.venv/bin:{ROOT}/.python-packages/bin:{ROOT}/node_modules/.bin:/usr/local/bin:/usr/bin:/bin',
        HOME=str(ROOT / '.atoms-data'), APP_DATA_DIR=str(ROOT / '.atoms-data'),
        PYTHONPATH=f'{ROOT}/.python-packages:{ROOT}', PYTHONNOUSERSITE='1',
        PYTHONDONTWRITEBYTECODE='1', PYTHONUNBUFFERED='1', NODE_ENV='production',
        BASE_PATH='/', PUBLIC_URL='/', HOST='127.0.0.1', NO_COLOR='1', CI='1',
        TMPDIR='/tmp', NPM_CONFIG_CACHE='/tmp/npm', PIP_CACHE_DIR='/tmp/pip',
        NODE_OPTIONS='--max-old-space-size=768', GOMAXPROCS='2', UV_THREADPOOL_SIZE='2')
    services = MANIFEST['services']
    for index, spec in enumerate(services):
        env[spec['port_env']] = str(9100 + index)
    try:
        for index, spec in enumerate(services):
            port = 9100 + index
            await launch(spec['name'], spec['command'], port, spec.get('ready_path', '/'), env)
            if spec['name'] == MANIFEST.get('api_service'):
                api_target = f'http://127.0.0.1:{port}'
        if MANIFEST.get('command'):
            await launch('application', MANIFEST['command'], 9200, MANIFEST.get('ready_path', '/'), env)
            main_target = 'http://127.0.0.1:9200'
        watcher = asyncio.create_task(watch_services())
        try:
            yield
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
    finally:
        for process in reversed(processes):
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    await asyncio.wait_for(process.wait(), 3)
                except (ProcessLookupError, TimeoutError):
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def authorize(value):
    if not hmac.compare_digest(value, TOKEN):
        raise HTTPException(403, 'Invalid release credential')


@app.get('/_atoms/health')
def health(request: Request):
    authorize(request.headers.get('x-release-token', ''))
    if any(process.returncode is not None for process in processes):
        raise HTTPException(503, 'Release service stopped')
    return {'ok': True, 'release_id': MANIFEST['release_id'], 'project_id': MANIFEST['project_id']}


@app.api_route('/application/{path:path}', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD'])
async def application(path: str, request: Request):
    authorize(request.headers.get('x-release-token', ''))
    target = (api_target or main_target) if path == 'api' or path.startswith('api/') else main_target
    if target:
        # Never forward the supervisor credential into an application.
        request.scope['headers'] = [(k, v) for k, v in request.scope['headers'] if k.lower() != b'x-release-token']
        return await forward_http(request, target + '/' + path, {},
                                  project_authorization=request.headers.get('authorization', ''))
    if request.method not in ('GET', 'HEAD') or path.startswith('api/'):
        raise HTTPException(404, 'Application route not found')
    dist = ROOT / 'dist'
    file = (dist / path).resolve()
    if not file.is_relative_to(dist.resolve()):
        raise HTTPException(404)
    if file.is_file():
        return FileResponse(file)
    if not Path(path).suffix and (dist / 'index.html').is_file():
        return FileResponse(dist / 'index.html')
    raise HTTPException(404)


@app.websocket('/application/{path:path}')
async def application_socket(socket: WebSocket, path: str):
    if not hmac.compare_digest(socket.headers.get('x-release-token', ''), TOKEN):
        await socket.close(code=1008)
        return
    target = (api_target or main_target) if path.startswith('api/') else main_target
    if not target:
        await socket.close(code=1008)
        return
    headers = {'Authorization': socket.headers['authorization']} if socket.headers.get('authorization') else {}
    await forward_websocket(socket, target.replace('http:', 'ws:') + '/' + path, headers)
