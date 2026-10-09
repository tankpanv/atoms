"""Schedule one isolated, persistent-workspace worker container per project."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import socket
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
import psycopg
from psycopg import sql
from fastapi import FastAPI, Header, HTTPException, Request, WebSocket
from fastapi.responses import Response
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from service_proxy import forward_http, forward_websocket
from billing_proxy import proxy_model, reconcile_loop, active_requests


DATABASE_URL = os.environ["DATABASE_URL"]
SECRET = os.getenv("AGENT_SECRET") or hashlib.sha256((DATABASE_URL + ":agent-control").encode()).hexdigest()
IMAGE = os.getenv("AGENT_IMAGE", "atoms-demo-backend")
VOLUME = os.getenv("WORKSPACE_VOLUME", "atoms-demo_agent_workspaces")
COMPOSE_PROJECT = os.getenv("COMPOSE_PROJECT_NAME", "atoms-demo")
DOCKER_SOCKET = os.getenv("DOCKER_SOCKET", "/var/run/docker.sock")
MAX_RUNNING = int(os.getenv("MAX_RUNNING_PROJECTS", "8"))
MAX_RETAINED = int(os.getenv("MAX_RETAINED_PROJECTS", "20"))
IDLE_SECONDS = int(os.getenv("PROJECT_IDLE_SECONDS", "120"))
COLD_SECONDS = int(os.getenv("PROJECT_COLD_SECONDS", "300"))
HOT_SECONDS = int(os.getenv("PROJECT_HOT_SECONDS", "3600"))
MEMORY_MB = int(os.getenv("PROJECT_MEMORY_MB", "4096"))
CPUS = float(os.getenv("PROJECT_CPUS", "2"))
PIDS_LIMIT = int(os.getenv("PROJECT_PIDS_LIMIT", "512"))
VISION_URL = os.getenv("VISION_URL", "http://vision:11434").rstrip("/")
VISION_MODEL = os.getenv("VISION_MODEL", "qwen2.5vl:3b")
_capacity_lock = asyncio.Lock()
_project_locks: dict[uuid.UUID, asyncio.Lock] = {}


def connection():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def authorize(value: str):
    if not hmac.compare_digest(value, SECRET):
        raise HTTPException(403, "Invalid agent service credential")


def worker_name(project_id: uuid.UUID):
    return f"atoms-project-{project_id.hex}"


def worker_cpu_set(project_id: uuid.UUID):
    # CPU quota alone leaves Node/Python seeing the host's entire CPU count.
    # Test pools then spawn dozens of processes despite a two-core quota.
    available = sorted(os.sched_getaffinity(0))
    count = max(1, min(len(available), int(CPUS)))
    start = project_id.int % len(available)
    return ','.join(str(available[(start+i) % len(available)]) for i in range(count))


def worker_token(project_id: uuid.UUID):
    return hmac.new(SECRET.encode(), b"worker-project:" + project_id.bytes, hashlib.sha256).hexdigest()


def worker_database_url(project_id: uuid.UUID):
    parsed = urlsplit(DATABASE_URL)
    role = f"atoms_project_{project_id.hex}"
    password = hmac.new(SECRET.encode(), b"database:" + project_id.bytes, hashlib.sha256).hexdigest()
    host = parsed.hostname or "db"
    port = f":{parsed.port}" if parsed.port else ""
    return urlunsplit((parsed.scheme, f"{quote(role)}:{quote(password)}@{host}{port}", parsed.path, parsed.query, parsed.fragment))


async def docker(method: str, path: str, payload: dict | None = None):
    transport = httpx.AsyncHTTPTransport(uds=DOCKER_SOCKET)
    # Removing a worker also tears down all service/browser subprocesses.
    # Docker may finish that cleanup after the old 30-second request deadline.
    timeout = httpx.Timeout(120 if method == 'DELETE' and path.startswith('/containers/') else 30, connect=5)
    async with httpx.AsyncClient(transport=transport, base_url="http://docker", timeout=timeout) as client:
        try:
            response = await client.request(method, path, json=payload)
        except httpx.HTTPError as exc:
            raise HTTPException(503, f"Docker engine unavailable during {method} {path}: {type(exc).__name__}: {exc}") from exc
    if response.status_code >= 400 and response.status_code != 404:
        raise HTTPException(502, f"Docker engine error: {response.text[:500]}")
    return response


async def containers():
    filters = quote(json.dumps({"label": ["atoms.role=project-worker", f"atoms.compose={COMPOSE_PROJECT}"]}))
    return (await docker("GET", f"/containers/json?all=1&filters={filters}")).json()


async def container_for(project_id: uuid.UUID):
    response = await docker("GET", f"/containers/{worker_name(project_id)}/json")
    return None if response.status_code == 404 else response.json()


def project_record(project_id: uuid.UUID):
    with connection() as conn:
        row = conn.execute("SELECT owner_id,workspace_path,status FROM projects WHERE id=%s", (project_id,)).fetchone()
        if not row or not row["owner_id"]:
            raise HTTPException(404, "Project not found")
        if row["status"] == "deleting":
            raise HTTPException(409, "Project is being deleted")
        if not row["workspace_path"]:
            root = Path("/workspaces")
            candidates = [f"projects/{project_id.hex}", f"users/{row['owner_id'].hex}/{project_id.hex}", project_id.hex]
            relative = next((value for value in candidates if (root / value).is_dir()), candidates[0])
            conn.execute("UPDATE projects SET workspace_path=%s WHERE id=%s AND workspace_path=''", (relative, project_id))
            row["workspace_path"] = relative
        relative = Path(row["workspace_path"])
        if relative.is_absolute() or ".." in relative.parts or relative.parts[-1] != project_id.hex:
            raise HTTPException(409, "Invalid recorded workspace path")
        root = Path("/workspaces").resolve()
        workspace = root / relative
        if workspace.is_symlink() or not workspace.resolve().is_relative_to(root):
            raise HTTPException(409, "Workspace must remain inside project storage")
        if not workspace.is_dir():
            raise HTTPException(404, "Recorded project workspace is unavailable")
        return row, relative


async def ensure_network(project_id: uuid.UUID):
    name = worker_name(project_id)
    network = await docker("GET", f"/networks/{name}")
    if network.status_code == 404:
        await docker("POST", "/networks/create", {"Name": name, "CheckDuplicate": True, "Driver": "bridge"})
    filters = quote(json.dumps({"label": [f"com.docker.compose.project={COMPOSE_PROJECT}", "com.docker.compose.service=db"]}))
    databases = (await docker("GET", f"/containers/json?filters={filters}")).json()
    if len(databases) != 1:
        raise HTTPException(503, "Database container is unavailable")
    members = ((await docker("GET", f"/networks/{name}")).json().get("Containers") or {})
    for container_id, alias in ((databases[0]["Id"], "db"), (socket.gethostname(), "agent-service")):
        if not any(member.startswith(container_id) or container_id.startswith(member) for member in members):
            await docker("POST", f"/networks/{name}/connect", {"Container": container_id, "EndpointConfig": {"Aliases": [alias]}})
    filters = quote(json.dumps({"label": [f"com.docker.compose.project={COMPOSE_PROJECT}", "com.docker.compose.service=preview"]}))
    previews = (await docker('GET', f'/containers/json?filters={filters}')).json()
    # Coordinator can start before the gateway during compose startup.
    # Browser preparation reconnects once the gateway is running.
    if len(previews) == 1 and previews[0]['Id'] not in members:
        await docker('POST', f'/networks/{name}/connect', {'Container': previews[0]['Id'], 'EndpointConfig': {'Aliases': ['preview']}})
    return name


async def remove_network(name: str):
    network = await docker("GET", f"/networks/{name}")
    if network.status_code != 404:
        for container_id in (network.json().get("Containers") or {}):
            await docker("POST", f"/networks/{name}/disconnect", {"Container": container_id, "Force": True})
        await docker("DELETE", f"/networks/{name}")


def project_states():
    with connection() as conn:
        rows = conn.execute("""
            SELECT p.id,p.workspace_path,p.dev_command,s.last_used_at,s.lease_expires_at,s.stopped_at,s.recent_open_count,
                   (p.status='restoring' OR EXISTS(SELECT 1 FROM agent_jobs j WHERE j.project_id=p.id AND j.status IN ('queued','running'))) AS busy
            FROM projects p LEFT JOIN project_runtime_state s ON s.project_id=p.id
        """).fetchall()
    return {row["id"]: row for row in rows}


def protected(state: dict | None):
    return bool(state and (state["busy"] or (state["lease_expires_at"] and state["lease_expires_at"] > datetime.now(timezone.utc))))


def idle_key(state: dict | None):
    return state["last_used_at"] if state and state["last_used_at"] else datetime.min.replace(tzinfo=timezone.utc)


def snapshot(project_id: uuid.UUID, state: dict | None):
    relative = state["workspace_path"] if state else ""
    config = Path("/workspaces") / relative / ".atoms-workspace.json" if relative else None
    dev_command = state["dev_command"] if state else ""
    if config and config.is_file():
        try:
            dev_command = str(json.loads(config.read_text()).get("dev", dev_command))
        except (OSError, ValueError, TypeError):
            pass
    with connection() as conn:
        version = conn.execute("SELECT COALESCE(MAX(version),0) AS version FROM project_versions WHERE project_id=%s", (project_id,)).fetchone()
        conn.execute("""
            INSERT INTO project_runtime_state(project_id,snapshot,stopped_at)
            VALUES(%s,%s,NOW()) ON CONFLICT(project_id) DO UPDATE SET snapshot=EXCLUDED.snapshot,stopped_at=NOW()
        """, (project_id, Jsonb({"schema": 1, "workspace_path": relative, "dev_command": dev_command,
                              "version": version["version"], "image": IMAGE,
                              "saved_at": datetime.now(timezone.utc).isoformat()})))
        conn.execute("DELETE FROM runtime_routes WHERE project_id=%s", (project_id,))


async def stop_worker(project_id: uuid.UUID, state: dict | None):
    current = await container_for(project_id)
    if current and current["State"]["Running"] and not protected(project_states().get(project_id)):
        await docker("POST", f"/containers/{worker_name(project_id)}/stop?t=10")
        snapshot(project_id, state)
        return True
    return False


async def remove_worker(project_id: uuid.UUID, *, preserve_network: bool = False):
    current = await container_for(project_id)
    if current:
        labels = current["Config"].get("Labels") or {}
        if labels.get("atoms.project") != project_id.hex or labels.get("atoms.compose") != COMPOSE_PROJECT:
            raise HTTPException(409, "Container ownership mismatch")
        await docker("DELETE", f"/containers/{worker_name(project_id)}?force=true&v=true")
    if not preserve_network:
        await remove_network(worker_name(project_id))


async def retire_legacy_workers():
    volume = await docker("GET", f"/volumes/{VOLUME}")
    if volume.status_code == 404:
        return
    root = str(Path(volume.json()["Mountpoint"]) / "users") + "/"
    filters = quote(json.dumps({"label": ["atoms.role=user-worker"]}))
    legacy = (await docker("GET", f"/containers/json?all=1&filters={filters}")).json()
    for item in legacy:
        current = (await docker("GET", f"/containers/{item['Id']}/json")).json()
        binds = current["HostConfig"].get("Binds") or []
        if not any(bind.startswith(root) for bind in binds):
            continue
        network = current["HostConfig"].get("NetworkMode", "")
        await docker("DELETE", f"/containers/{item['Id']}?force=true")
        if network.startswith("atoms-user-"):
            await remove_network(network)
        owner = item["Labels"].get("atoms.user")
        if owner and len(owner) == 32 and all(character in "0123456789abcdef" for character in owner):
            role_name = f"atoms_user_{owner}"
            with connection() as conn:
                if conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role_name,)).fetchone():
                    role = sql.Identifier(role_name)
                    conn.execute(sql.SQL("DROP OWNED BY {}").format(role))
                    conn.execute(sql.SQL("DROP ROLE {}").format(role))


async def reserve_capacity(project_id: uuid.UUID, creating: bool):
    listed = await containers()
    states = project_states()
    running = [item for item in listed if item["State"] == "running" and item["Labels"].get("atoms.project") != project_id.hex]
    while len(running) >= MAX_RUNNING:
        candidates = sorted((item for item in running if not protected(states.get(uuid.UUID(hex=item["Labels"]["atoms.project"])))),
                            key=lambda item: idle_key(states.get(uuid.UUID(hex=item["Labels"]["atoms.project"]))))
        if not candidates:
            raise HTTPException(503, "All project containers are active; no safe eviction candidate")
        victim = candidates[0]
        victim_id = uuid.UUID(hex=victim["Labels"]["atoms.project"])
        if not await stop_worker(victim_id, states.get(victim_id)):
            raise HTTPException(503, "Project became active during capacity recovery")
        running.remove(victim)
    if creating:
        listed = await containers()
        while len(listed) >= MAX_RETAINED:
            candidates = sorted((item for item in listed if item["State"] != "running" and
                                 item["Labels"].get("atoms.project") != project_id.hex and
                                 not protected(states.get(uuid.UUID(hex=item["Labels"]["atoms.project"])))),
                                key=lambda item: idle_key(states.get(uuid.UUID(hex=item["Labels"]["atoms.project"]))))
            if not candidates:
                raise HTTPException(503, "Project container retention limit reached")
            victim = candidates[0]
            await remove_worker(uuid.UUID(hex=victim["Labels"]["atoms.project"]))
            listed.remove(victim)


async def ensure_worker(project_id: uuid.UUID, *, allow_idle_upgrade: bool = False, fresh_restore: bool = False):
    async with _capacity_lock:
        async with _project_locks.setdefault(project_id, asyncio.Lock()):
            volume = await docker("GET", f"/volumes/{VOLUME}")
            if volume.status_code == 404:
                raise HTTPException(503, "Workspace volume is unavailable")
            record, relative = project_record(project_id)
            host_path = Path(volume.json()["Mountpoint"]) / relative
            network = await ensure_network(project_id)
            current = await container_for(project_id)
            if fresh_restore:
                with connection() as conn:
                    busy = conn.execute("SELECT 1 FROM agent_jobs WHERE project_id=%s AND status IN ('queued','running')", (project_id,)).fetchone()
                if busy or record['status'] in {'running', 'queued', 'restoring'}:
                    raise HTTPException(409, '请等待当前任务或工作区恢复完成')
                # A version switch never inherits the previous worker process,
                # runtime tokens, in-memory sessions or container filesystem.
                # Recreate the application container, keeping shared services
                # attached. Disconnecting the coordinator/gateway network here
                # also drops the HTTP connection accepting this restoration.
                await remove_worker(project_id, preserve_network=True)
                with connection() as conn:
                    conn.execute('DELETE FROM runtime_routes WHERE project_id=%s', (project_id,))
                current = None
                network = await ensure_network(project_id)
            image = await docker("GET", f"/images/{IMAGE}/json")
            if image.status_code == 404:
                raise HTTPException(503, "Project worker image is unavailable")
            bind = f"{host_path}:/workspaces/{project_id.hex}:rw"
            service = await docker('GET', f'/containers/{socket.gethostname()}/json')
            model_path = os.getenv('MODEL_LIST_PATH', '/model_list')
            model_mount = next((mount for mount in service.json().get('Mounts', [])
                                if mount['Destination'] == model_path), None)
            if not model_mount:
                raise HTTPException(503, 'Canonical model_list mount is unavailable')
            model_bind = f"{model_mount['Source']}:/model_list:ro"
            from build_tiers import BUDGET_ENV_KEYS, worker_budget_changed
            obsolete = bool(current and (current["Image"] != image.json()["Id"] or
                           worker_budget_changed(current["Config"].get("Env")) or
                           bind not in (current["HostConfig"].get("Binds") or []) or
                           model_bind not in (current["HostConfig"].get("Binds") or []) or
                           current["HostConfig"].get("NetworkMode") != network or
                           not current["HostConfig"].get("Init") or
                           f"PROJECT_ID={project_id.hex}" not in (current["Config"].get("Env") or []) or
                           f"DATABASE_URL={worker_database_url(project_id)}" not in (current["Config"].get("Env") or [])))
            queued_upgrade = False
            if obsolete:
                with connection() as conn:
                    statuses = conn.execute(
                        "SELECT status FROM agent_jobs WHERE project_id=%s AND status IN ('queued','running')",
                        (project_id,)).fetchall()
                queued_upgrade = any(row["status"] == "queued" for row in statuses) and not any(
                    row["status"] == "running" for row in statuses)
                # An explicit build/edit/restore must adopt installed fixes even
                # while a browser preview lease keeps the idle container warm.
                queued_upgrade = queued_upgrade or (allow_idle_upgrade and not statuses and record['status'] != 'restoring')
            if obsolete and (queued_upgrade or not (
                    current["State"]["Running"] and protected(project_states().get(project_id)))):
                await remove_worker(project_id, preserve_network=True)
                current = None
                network = await ensure_network(project_id)
            mode = "reused" if current and current["State"]["Running"] else "resumed" if current else "restored"
            if mode != "reused":
                await reserve_capacity(project_id, creating=current is None)
            if not current:
                from project_database import credentials
                app_schema, _, _, app_url = credentials(project_id, DATABASE_URL, SECRET)
                env = {
                    "APP_DATABASE_URL": app_url, "APP_DATABASE_SCHEMA": app_schema,
                    "PROJECT_ID": project_id.hex, "USER_ID": record["owner_id"].hex,
                    "WORKER_TOKEN": worker_token(project_id), "WORKSPACE_ROOT": "/workspaces",
                    "DATABASE_URL": worker_database_url(project_id),
                    "AI_API_KEY": "",
                    "AI_BASE_URL": os.getenv("AI_BASE_URL", "https://openrouter.ai/api/v1"),
                    "AI_MODEL": os.getenv("AI_MODEL", "openai/gpt-6-luna"),
                    "MODEL_LIST_PATH": "/model_list",
                    "PREVIEW_GATEWAY_URL": "http://preview:8002",
                    "STATE_CHECKPOINT_URL": "http://agent-service:9001",
                    "PROJECT_RSS_LIMIT_MB": os.getenv("PROJECT_RSS_LIMIT_MB", str(max(256, MEMORY_MB - 256))),
                    "PYTHONDONTWRITEBYTECODE": "1", "HOME": "/workspaces",
                }
                for setting in ("AI_UNDERSTAND_MODEL", "AI_PLAN_MODEL", "AI_IMPLEMENT_MODEL",
                                "AI_REVIEW_MODEL", "AGENT_CONTEXT_TOKENS", "AGENT_TIMEOUT_SECONDS",
                                *BUDGET_ENV_KEYS):
                    if os.getenv(setting):
                        env[setting] = os.environ[setting]
                config = {
                    "Image": IMAGE, "Cmd": ["uvicorn", "project_worker:app", "--host", "0.0.0.0", "--port", "9000"],
                    "WorkingDir": "/app", "User": "0:0", "Env": [f"{key}={value}" for key, value in env.items()],
                    "Labels": {"atoms.project": project_id.hex, "atoms.owner": record["owner_id"].hex,
                               "atoms.role": "project-worker", "atoms.compose": COMPOSE_PROJECT},
                    "ExposedPorts": {"9000/tcp": {}},
                    "HostConfig": {
                        "Binds": [bind, model_bind], "NetworkMode": network, "Memory": MEMORY_MB * 1024 * 1024,
                        "NanoCpus": int(CPUS * 1_000_000_000), "PidsLimit": PIDS_LIMIT, "Init": True,
                        "CpusetCpus": worker_cpu_set(project_id),
                        "CapDrop": ["ALL"], "CapAdd": ["CHOWN", "SETUID", "SETGID", "DAC_OVERRIDE", "KILL"],
                        "SecurityOpt": ["no-new-privileges:true"], "ReadonlyRootfs": True,
                        "Tmpfs": {"/tmp": "rw,nosuid,size=134217728"},
                    },
                }
                created = await docker("POST", f"/containers/create?name={worker_name(project_id)}", config)
                if created.status_code != 201:
                    raise HTTPException(502, "Could not create project worker")
            if mode != "reused":
                started = await docker("POST", f"/containers/{worker_name(project_id)}/start")
                if started.status_code not in (204, 304):
                    raise HTTPException(502, "Could not start project worker")
            url = f"http://{worker_name(project_id)}:9000"
            async with httpx.AsyncClient(timeout=2) as client:
                for _ in range(60):
                    try:
                        response = await client.get(url + "/health")
                        if response.status_code == 200 and response.json().get("project_id") == str(project_id):
                            with connection() as conn:
                                conn.execute("""
                                    INSERT INTO project_runtime_state(project_id,last_used_at,stopped_at)
                                    VALUES(%s,NOW(),NULL) ON CONFLICT(project_id) DO UPDATE SET
                                    last_used_at=NOW(),stopped_at=NULL
                                """, (project_id,))
                            return url, mode
                    except (httpx.HTTPError, ValueError):
                        pass
                    await asyncio.sleep(0.25)
            raise HTTPException(503, "Project worker did not become healthy")


async def running_worker(project_id: uuid.UUID):
    current = await container_for(project_id)
    if not current or not current["State"]["Running"]:
        raise HTTPException(404, "Project runtime is stopped")
    return f"http://{worker_name(project_id)}:9000"


async def reconnect_project_networks():
    for item in await containers():
        if item["State"] == "running":
            await ensure_network(uuid.UUID(hex=item["Labels"]["atoms.project"]))


async def reap_once():
    async with _capacity_lock:
        states = project_states()
        now = datetime.now(timezone.utc)
        for item in await containers():
            project_id = uuid.UUID(hex=item["Labels"]["atoms.project"])
            state = states.get(project_id)
            if not state:
                await remove_worker(project_id)
                continue
            if protected(state):
                continue
            if item["State"] == "running" and (now - idle_key(state)).total_seconds() >= IDLE_SECONDS:
                async with _project_locks.setdefault(project_id, asyncio.Lock()):
                    await stop_worker(project_id, state)
            elif item["State"] != "running" and not state["stopped_at"]:
                snapshot(project_id, state)
            elif item["State"] != "running" and state["stopped_at"]:
                retention = HOT_SECONDS if state["recent_open_count"] >= 2 else COLD_SECONDS
                if (now - state["stopped_at"]).total_seconds() >= retention:
                    async with _project_locks.setdefault(project_id, asyncio.Lock()):
                        if not protected(project_states().get(project_id)):
                            await remove_worker(project_id)
        listed = await containers()
        running = [item for item in listed if item["State"] == "running"]
        while len(running) > MAX_RUNNING:
            candidates = sorted((item for item in running if not protected(states.get(uuid.UUID(hex=item["Labels"]["atoms.project"])))),
                                key=lambda item: idle_key(states.get(uuid.UUID(hex=item["Labels"]["atoms.project"]))))
            if not candidates:
                break
            victim = candidates[0]
            project_id = uuid.UUID(hex=victim["Labels"]["atoms.project"])
            async with _project_locks.setdefault(project_id, asyncio.Lock()):
                if not await stop_worker(project_id, states.get(project_id)):
                    break
            running.remove(victim)
        listed = await containers()
        while len(listed) > MAX_RETAINED:
            candidates = sorted((item for item in listed if item["State"] != "running" and
                                 not protected(states.get(uuid.UUID(hex=item["Labels"]["atoms.project"])))),
                                key=lambda item: idle_key(states.get(uuid.UUID(hex=item["Labels"]["atoms.project"]))))
            if not candidates:
                break
            victim = candidates[0]
            project_id = uuid.UUID(hex=victim["Labels"]["atoms.project"])
            async with _project_locks.setdefault(project_id, asyncio.Lock()):
                if protected(project_states().get(project_id)):
                    break
                await remove_worker(project_id)
            listed.remove(victim)


async def reap_loop():
    while True:
        await asyncio.sleep(10)
        try:
            await reap_once()
        except Exception as exc:
            print(f"Project reaper error: {exc}", flush=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await retire_legacy_workers()
    await reconnect_project_networks()
    import sys
    from project_publication import recover_publications, migrate_legacy_publications
    async def recover_releases():
        await recover_publications(sys.modules[__name__])
        await migrate_legacy_publications(sys.modules[__name__])
    publication_recovery = asyncio.create_task(recover_releases())
    reaper = asyncio.create_task(reap_loop())
    billing_reconciler = asyncio.create_task(reconcile_loop())
    try:
        yield
    finally:
        reaper.cancel()
        billing_reconciler.cancel()
        from project_publication import tasks as publication_tasks
        publication_recovery.cancel()
        for task in list(publication_tasks.values()):
            task.cancel()
        await asyncio.gather(reaper, billing_reconciler, publication_recovery, *list(publication_tasks.values()), *list(active_requests.values()), return_exceptions=True)


app = FastAPI(title="Atoms Agent Service", lifespan=lifespan)


@app.post('/projects/{project_id}/preview-check/{token}')
async def prepare_browser_preview(project_id: uuid.UUID, token: str, x_worker_token: str = Header(default='')):
    if not hmac.compare_digest(x_worker_token, worker_token(project_id)):
        raise HTTPException(403, 'Invalid project worker credential')
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,64}', token):
        raise HTTPException(422, 'Invalid runtime token')
    project_record(project_id)
    # Verify the token against the running worker, not caller-supplied routes.
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.post(f'http://{worker_name(project_id)}:9000/projects/{project_id}/invoke/runtime_status',
                                     headers={'X-Worker-Token': worker_token(project_id)}, json={})
        response.raise_for_status()
    status = response.json().get('status') or {}
    if not status.get('running') or status.get('url') != f'/api/runtime/{token}/':
        raise HTTPException(409, 'Runtime changed or stopped before browser acceptance')
    await ensure_network(project_id)
    with connection() as conn:
        conn.execute('DELETE FROM runtime_routes WHERE project_id=%s', (project_id,))
        conn.execute('INSERT INTO runtime_routes(token_hash,project_id) VALUES(%s,%s) ON CONFLICT(token_hash) DO UPDATE SET project_id=EXCLUDED.project_id', (hashlib.sha256(token.encode()).hexdigest(), project_id))
    return {'url': f'http://preview:8002/api/runtime/{token}/'}


@app.post('/projects/{project_id}/model')
async def billed_model(project_id: uuid.UUID, request: Request, x_worker_token: str = Header(default='')):
    if not hmac.compare_digest(x_worker_token, worker_token(project_id)):
        raise HTTPException(403, 'Invalid project worker credential')
    return await proxy_model(project_id, await request.json())


@app.get('/projects/{project_id}/models')
async def worker_models(project_id: uuid.UUID, x_worker_token: str = Header(default='')):
    if not hmac.compare_digest(x_worker_token, worker_token(project_id)):
        raise HTTPException(403)
    from model_catalog import live_catalog
    models = await live_catalog()
    return {'data': [{'id': m['id'], 'architecture': {'input_modalities': ['text', 'image'] if m['image_input'] else ['text']}} for m in models]}


class VisionRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    image: str = Field(min_length=1, max_length=7_000_000)


@app.post("/projects/{project_id}/vision")
async def analyze_project_image(project_id: uuid.UUID, data: VisionRequest, x_worker_token: str = Header(default="")):
    if not hmac.compare_digest(x_worker_token, worker_token(project_id)):
        raise HTTPException(403, "Invalid project worker credential")
    with connection() as conn:
        if not conn.execute("SELECT 1 FROM projects WHERE id=%s", (project_id,)).fetchone():
            raise HTTPException(404, "Project not found")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(240, connect=5)) as client:
            response = await client.post(f"{VISION_URL}/api/chat", json={
                "model": VISION_MODEL, "stream": False, "keep_alive": "5m",
                "messages": [{"role": "user", "content": data.prompt, "images": [data.image]}],
                "options": {"temperature": 0.1, "num_ctx": 4096, "num_thread": 4, "num_predict": 350},
            })
        response.raise_for_status()
        description = response.json().get("message", {}).get("content", "").strip()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(503, f"本地图片识别服务不可用：{str(exc)[:300]}") from exc
    if not description:
        raise HTTPException(502, "本地图片识别服务未返回内容")
    return {"model": VISION_MODEL, "description": description}


@app.get("/health")
async def health():
    return {"ok": True, "role": "agent-service"}


_restore_invocation_locks: dict[uuid.UUID, asyncio.Lock] = {}


@app.post("/projects/{project_id}/invoke/{operation}")
async def invoke(project_id: uuid.UUID, operation: str, request: Request, x_agent_secret: str = Header(default="")):
    authorize(x_agent_secret)
    if operation not in {"open", "run", "runtime_start", "runtime_stop", "runtime_status", "command", "build", "restore"}:
        raise HTTPException(404, "Unknown operation")
    if operation == 'restore':
        # Hold through the ACK: after release the durable status is restoring,
        # so a duplicate request cannot destroy the newly created worker.
        async with _restore_invocation_locks.setdefault(project_id, asyncio.Lock()):
            payload = await request.json()
            if not isinstance(payload, dict):
                raise HTTPException(422, '版本请求格式无效')
            version = payload.get('version')
            if not isinstance(version, int) or isinstance(version, bool) or version < 1:
                raise HTTPException(422, '版本编号无效')
            with connection() as conn:
                selected = conn.execute('SELECT runtime_state FROM project_versions WHERE project_id=%s AND version=%s', (project_id, version)).fetchone()
                if not selected:
                    raise HTTPException(404, '版本不存在')
                if not selected['runtime_state'].get('data'):
                    raise HTTPException(422, '此旧版本没有数据库和浏览器数据快照，无法完整还原；当前代码和数据未修改')
            # Drain platform writes before tearing down their worker. The
            # initiating POST holds only the deletion lock, so it cannot
            # block this exclusive lock. Release after the worker ACK: its
            # durable restoring status then blocks new mutations until the
            # background task takes this same lock for the full restoration.
            with connection() as guard:
                locked=guard.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,19)) AS acquired',(str(project_id),)).fetchone()['acquired']
                guard.commit()
                if not locked:
                    raise HTTPException(409,'当前仍有文件或数据写入，请等待完成后再还原；工作区未修改')
                try:
                    return await invoke_worker(project_id, operation, request, fresh_restore=True)
                finally:
                    guard.rollback()
                    guard.execute('SELECT pg_advisory_unlock(hashtextextended(%s,19))',(str(project_id),))
    return await invoke_worker(project_id, operation, request)


async def invoke_worker(project_id, operation, request, *, fresh_restore=False):
    options = {'allow_idle_upgrade': operation in {'command', 'build', 'restore'}}
    if fresh_restore:
        options['fresh_restore'] = True
    url, mode = await ensure_worker(project_id, **options)
    if operation == "open":
        return {"ok": True, "mode": mode}
    async with httpx.AsyncClient(timeout=httpx.Timeout(210, read=210)) as client:
        try:
            response = await client.post(f"{url}/projects/{project_id}/invoke/{operation}", content=await request.body(),
                                         headers={"X-Worker-Token": worker_token(project_id), "Content-Type": "application/json"})
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"Project worker unavailable: {exc}") from exc
    return Response(response.content, status_code=response.status_code, media_type="application/json")


@app.post('/projects/{project_id}/data-checkpoint')
async def data_checkpoint(project_id: uuid.UUID, request: Request, x_worker_token: str = Header(default='')):
    if not hmac.compare_digest(x_worker_token, worker_token(project_id)):
        raise HTTPException(403, 'Invalid project checkpoint credential')
    with connection() as conn:
        project = conn.execute('SELECT status FROM projects WHERE id=%s',(project_id,)).fetchone()
    if not project or project['status']=='deleting':
        raise HTTPException(404, 'Project unavailable')
    payload = await request.json()
    import sys
    from project_state_snapshots import capture_state, restore_state, verify_state, checked_snapshot
    try:
        if payload.get('operation')=='capture':
            return await capture_state(project_id,sys.modules[__name__])
        if payload.get('operation')=='validate':
            with connection() as conn:
                checked_snapshot(conn,project_id,payload.get('id'))
            return {'valid':True}
        if payload.get('operation')=='restore':
            if project['status']!='restoring':
                raise HTTPException(409,'数据恢复只能在版本还原过程中执行')
            return await restore_state(project_id,payload.get('id'),sys.modules[__name__])
        if payload.get('operation')=='verify':
            return verify_state(project_id,payload.get('id'),sys.modules[__name__])
        raise HTTPException(422,'数据快照操作无效')
    except (ValueError, psycopg.Error) as exc:
        raise HTTPException(422, str(exc)[:1500]) from exc


@app.delete("/projects/{project_id}")
async def remove_project(project_id: uuid.UUID, x_agent_secret: str = Header(default="")):
    authorize(x_agent_secret)
    import sys
    from project_publication import stop_publication
    await stop_publication(sys.modules[__name__], project_id, delete=True)
    async with _capacity_lock:
        async with _project_locks.setdefault(project_id, asyncio.Lock()):
            await remove_worker(project_id)
    return {"ok": True}


@app.post('/projects/{project_id}/publication')
async def publish_project(project_id: uuid.UUID, request: Request, x_agent_secret: str = Header(default='')):
    authorize(x_agent_secret)
    import sys
    from project_publication import enqueue
    return await enqueue(sys.modules[__name__], project_id, await request.json())


@app.delete('/projects/{project_id}/publication')
async def unpublish_project(project_id: uuid.UUID, x_agent_secret: str = Header(default='')):
    authorize(x_agent_secret)
    import sys
    from project_publication import stop_publication
    await stop_publication(sys.modules[__name__], project_id)
    return {'ok': True}


@app.post('/projects/{project_id}/releases/{release_id}/activate')
async def rollback_publication(project_id: uuid.UUID, release_id: uuid.UUID, x_agent_secret: str = Header(default='')):
    authorize(x_agent_secret)
    import sys
    from project_publication import row, start_release, activate, retire_containers, describe
    service = sys.modules[__name__]
    release = row(service, release_id)
    if not release or release['project_id'] != project_id or release['status'] not in {'retired', 'active'}:
        raise HTTPException(404, '已验证发布版本不存在')
    with connection() as guard:
        if not guard.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,38)) AS locked', (str(project_id),)).fetchone()['locked']:
            raise HTTPException(409, '已有发布操作正在进行')
        try:
            await start_release(service, release)
            activate(service, release)
        finally:
            guard.execute('SELECT pg_advisory_unlock(hashtextextended(%s,38))', (str(project_id),))
    asyncio.create_task(retire_containers(service, project_id, release_id))
    return describe(row(service, release_id))


@app.api_route('/projects/{project_id}/published/{path:path}', methods=['GET','POST','PUT','PATCH','DELETE','OPTIONS','HEAD'])
async def published_application(project_id: uuid.UUID, path: str, request: Request, x_agent_secret: str = Header(default='')):
    authorize(x_agent_secret)
    import sys
    from project_publication import active_release, release_name, token
    service = sys.modules[__name__]
    release = await active_release(service, project_id)
    return await forward_http(request,
        f'http://{release_name(project_id, release["id"])}:9000/application/{quote(path, safe="/")}',
        {'X-Release-Token': token(service, release['id'])}, project_authorization=request.headers.get('authorization', ''))


@app.websocket('/projects/{project_id}/published/{path:path}')
async def published_socket(socket: WebSocket, project_id: uuid.UUID, path: str):
    if not hmac.compare_digest(socket.headers.get('x-agent-secret', ''), SECRET):
        await socket.close(code=1008)
        return
    import sys
    from project_publication import active_release, release_name, token
    service = sys.modules[__name__]
    try:
        release = await active_release(service, project_id)
    except HTTPException:
        await socket.close(code=1008)
        return
    headers = {'X-Release-Token': token(service, release['id'])}
    if socket.headers.get('authorization'):
        headers['Authorization'] = socket.headers['authorization']
    await forward_websocket(socket,
        f'ws://{release_name(project_id, release["id"])}:9000/application/{quote(path, safe="/")}', headers)


@app.api_route("/projects/{project_id}/preview/{token}/", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
@app.api_route("/projects/{project_id}/preview/{token}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def preview(project_id: uuid.UUID, token: str, request: Request, path: str = "", x_agent_secret: str = Header(default="")):
    authorize(x_agent_secret)
    url = await running_worker(project_id)
    return await forward_http(request, f"{url}/projects/{project_id}/api/runtime/{token}/{path}", {},
                              project_authorization=request.headers.get("authorization", ""))


@app.websocket("/projects/{project_id}/preview/{token}/")
@app.websocket("/projects/{project_id}/preview/{token}/{path:path}")
async def preview_websocket(websocket: WebSocket, project_id: uuid.UUID, token: str, path: str = ""):
    if not hmac.compare_digest(websocket.headers.get("x-agent-secret", ""), SECRET):
        await websocket.close(code=1008)
        return
    try:
        url = await running_worker(project_id)
    except HTTPException:
        await websocket.close(code=1011)
        return
    await forward_websocket(websocket, f"{url.replace('http://', 'ws://')}/projects/{project_id}/api/runtime/{token}/{path}", {})

class CloneDatabaseInput(BaseModel):
    source: uuid.UUID
    target: uuid.UUID
    include_data: bool = False

@app.post('/control/clone-database')
async def copy_project_database(data: CloneDatabaseInput, x_agent_secret: str = Header(default='')):
    authorize(x_agent_secret)
    with connection() as conn:
        projects = conn.execute('SELECT id,owner_id,published,visibility FROM projects WHERE id IN (%s,%s)', (data.source,data.target)).fetchall()
        records = {item['id']: item for item in projects}
        source, target = records.get(data.source), records.get(data.target)
        if data.source == data.target or not source or not target:
            raise HTTPException(403, '拒绝克隆无效项目数据库')
        if source['owner_id'] != target['owner_id'] and (data.include_data or not source['published'] or source['visibility'] != 'public'):
            raise HTTPException(403, '公开项目仅允许克隆数据库结构')
    from clone_database import clone_database
    try:
        return await clone_database(data.source,data.target,connection,docker,COMPOSE_PROJECT,DATABASE_URL,data.include_data)
    except Exception as error:
        # Never expose dump output, application records, or credentials.
        raise HTTPException(409, '数据库克隆失败，已停止克隆，请检查数据库结构及跨 Schema 引用') from error
