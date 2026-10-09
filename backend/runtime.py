"""Long-running HTTP development servers for individual project workspaces."""

from __future__ import annotations

import asyncio
import json
import os
import resource
import secrets
import signal
import socket
import uuid
import re
from dataclasses import dataclass, field

import httpx

from agent import ensure_workspace, project_uid, run_command, set_workspace_owner


@dataclass
class Service:
    name: str
    port: int
    command: str
    process: asyncio.subprocess.Process
    output: str = ""


@dataclass
class Runtime:
    project_id: uuid.UUID
    token: str
    port: int
    command: str
    base_aware: bool
    process: asyncio.subprocess.Process
    output: str = ""
    services: list[Service] = field(default_factory=list)
    configuration: str = ""
    source_version: str = ""
    ready: bool = False

    @property
    def prefix(self):
        return f"/api/runtime/{self.token}"


_runtimes: dict[uuid.UUID, Runtime] = {}
_tokens: dict[str, Runtime] = {}
_locks: dict[uuid.UUID, asyncio.Lock] = {}
_reserved_ports: set[int] = set()


def base_path_aware(root, command):
    """The gateway must retain the prefix consumed by the actual Vite config.

    Explicit dev commands often pass $PORT but let vite.config read BASE_PATH.
    Checking only the command made readiness pass while every API call failed.
    A workspace override covers less conventional runners/configurations.
    """
    config = root / '.atoms-workspace.json'
    if config.exists():
        value = json.loads(config.read_text()).get('base_aware')
        if value is not None:
            if not isinstance(value, bool):
                raise RuntimeError('工作区 base_aware 必须为布尔值')
            return value
    if '$BASE_PATH' in command:
        return True
    if not re.search(r'\b(?:npm|pnpm|yarn|vite)\b', command):
        return False
    for directory in (root, root / 'frontend'):
        for suffix in ('ts', 'js', 'mts', 'mjs', 'cts', 'cjs'):
            path = directory / ('vite.config.' + suffix)
            if path.is_file() and re.search(r"process\.env(?:\.BASE_PATH|\[\s*['\"]BASE_PATH['\"]\s*\])", path.read_text()):
                return True
    return False


def detected_command(project_id: uuid.UUID):
    root = ensure_workspace(project_id)
    config_file = root / ".atoms-workspace.json"
    if config_file.exists():
        try:
            from execution_contract import workspace_commands, validate_command
            configured = workspace_commands(root).get("dev", "")
            if configured: validate_command(configured)
        except (ValueError, OSError) as exc:
            raise RuntimeError(f"工作区配置无效：{exc}") from exc
        if configured:
            return configured, base_path_aware(root, configured)
    package = root / "package.json"
    if package.exists():
        try:
            metadata = json.loads(package.read_text())
            scripts = metadata.get("scripts", {})
        except (ValueError, OSError):
            scripts = {}
            metadata = {}
        if "dev" in scripts:
            if "vite" in scripts["dev"]:
                return "npm run dev -- --host 127.0.0.1 --port $PORT --strictPort --base $BASE_PATH", True
            return "npm run dev", False
        if "start" in scripts:
            return "npm run start", False
        if "vite" in {**metadata.get("dependencies", {}), **metadata.get("devDependencies", {})}:
            return "node_modules/.bin/vite --host 127.0.0.1 --port $PORT --strictPort --base $BASE_PATH", True
    return "", False


def _free_port():
    for _ in range(100):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            port = server.getsockname()[1]
        if port not in _reserved_ports:
            _reserved_ports.add(port)
            return port
    raise RuntimeError("没有可用的项目预览端口")


def _workspace_user(uid: int):
    if os.geteuid() != uid:
        os.setgroups([])
        os.setgid(uid)
        os.setuid(uid)
    resource.setrlimit(resource.RLIMIT_NPROC, (256, 256))
    resource.setrlimit(resource.RLIMIT_CPU, (3600, 3600))
    resource.setrlimit(resource.RLIMIT_NOFILE, (1024, 1024))


async def _capture_output(runtime: Runtime):
    if not runtime.process.stdout:
        return
    while data := await runtime.process.stdout.read(4096):
        runtime.output = (runtime.output + data.decode(errors="replace"))[-16_000:]


def configured_services(root):
    path = root / ".atoms-workspace.json"
    settings = json.loads(path.read_text()) if path.exists() else {}
    services = settings.get("services", [])
    if not isinstance(services, list) or len(services) > 8:
        raise ValueError("services 必须为最多 8 个服务的列表")
    names, variables = set(), set()
    for service in services:
        if not isinstance(service, dict):
            raise ValueError("服务配置必须为对象")
        name, variable = service.get("name", ""), service.get("port_env", "")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,30}", name) or name in names:
            raise ValueError("服务名无效或重复")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*_PORT", variable) or variable in variables:
            raise ValueError("服务必须指定唯一的端口变量，例如 API_PORT")
        if not isinstance(service.get("command"), str) or not service["command"].strip():
            raise ValueError("服务缺少启动命令")
        path = service.get("ready_path", "/")
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
            raise ValueError("服务 ready_path 必须是本地 HTTP 路径")
        names.add(name)
        variables.add(variable)
    return services


async def _terminate(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), 3)
    except TimeoutError:
        pass
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


async def _wait_ready(process, port, path, output):
    last_probe = "尚未收到 HTTP 响应"
    async with httpx.AsyncClient(timeout=2, follow_redirects=False) as client:
        for _ in range(120):
            if process.returncode is not None:
                break
            try:
                response = await client.get(f"http://127.0.0.1:{port}{path}")
                last_probe = f"GET {path} → HTTP {response.status_code}: {response.text[:280]}"
                if 200 <= response.status_code < 400:
                    return
            except httpx.HTTPError as exc:
                last_probe = f"GET {path} → {type(exc).__name__}: {str(exc)[:280]}"
            await asyncio.sleep(0.25)
    raise RuntimeError(f"服务启动失败：{last_probe}\n{output()[-1600:] or '服务未在就绪等待期内响应'}")


async def stop_runtime(project_id: uuid.UUID):
    runtime = _runtimes.pop(project_id, None)
    if not runtime:
        return
    _tokens.pop(runtime.token, None)
    _reserved_ports.discard(runtime.port)
    await _terminate(runtime.process)
    for service in reversed(runtime.services):
        await _terminate(service.process)
        _reserved_ports.discard(service.port)


async def stop_all_runtimes():
    for project_id in list(_runtimes):
        await stop_runtime(project_id)


async def start_runtime(project_id: uuid.UUID, configured_command: str = "", restart: bool = False):
    async with _locks.setdefault(project_id, asyncio.Lock()):
        current = _runtimes.get(project_id)
        automatic, base_aware = detected_command(project_id)
        command = configured_command or automatic
        root = ensure_workspace(project_id)
        try:
            services = configured_services(root)
        except (ValueError, OSError) as exc:
            raise RuntimeError(f"工作区服务配置无效：{exc}") from exc
        from agent import snapshot_files
        from agent_harness import source_digest
        source_version = source_digest(snapshot_files(root))
        configuration = json.dumps(services, sort_keys=True)
        if (current and current.process.returncode is None and
                all(service.process.returncode is None for service in current.services) and not restart and
                command == current.command and configuration == current.configuration and source_version == current.source_version):
            return current
        await stop_runtime(project_id)
        if not command:
            return None
        from agent import ensure_python_dependencies
        python_code, python_output = await ensure_python_dependencies(project_id)
        if python_code:
            raise RuntimeError(f'Python 依赖准备失败：{python_output[-3000:]}')
        if configured_command:
            base_aware = base_path_aware(root, configured_command)
        if (root / "package.json").exists() and not (root / "node_modules").exists():
            code, output = await run_command(project_id, ["npm", "install", "--ignore-scripts", "--no-audit", "--no-fund"], 120)
            if code:
                raise RuntimeError(f"依赖安装失败：{output[-1200:]}")
        uid = project_uid(project_id)
        set_workspace_owner(root, uid)
        cache = root / ".npm-cache"
        cache.mkdir(exist_ok=True)
        if cache.stat().st_uid != uid:
            os.chown(cache, uid, uid)
        token = secrets.token_urlsafe(32)
        port = _free_port()
        prefix = f"/api/runtime/{token}"
        env = {"PATH": f"{root / 'node_modules' / '.bin'}:/usr/local/bin:/usr/bin:/bin",
               "HOME": str(root), "NPM_CONFIG_CACHE": str(cache), "NODE_ENV": "development",
               "NO_COLOR": "1", "CI": "1", "PORT": str(port), "HOST": "127.0.0.1",
               "PYTHONPATH": f"{root / '.python-packages'}:{root}",
               "APP_DATA_DIR": str(root / ".atoms-data"),
               "BASE_PATH": prefix + "/", "PUBLIC_URL": prefix + "/", "PYTHONUNBUFFERED": "1",
               "__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS": "atoms-preview.test",
               "NODE_OPTIONS": "--max-old-space-size=768", "GOMAXPROCS": "2",
               "RAYON_NUM_THREADS": "2", "TOKIO_WORKER_THREADS": "2", "UV_THREADPOOL_SIZE": "2"}
        from project_database import environment as database_environment
        from project_python import environment as python_environment
        env.update(python_environment(root, uid))
        env.update(database_environment(project_id))
        from project_secrets import environment as secret_environment
        env.update(secret_environment(root, uid))
        started_services = []
        try:
            for spec in services:
                service_port = _free_port()
                env[spec["port_env"]] = str(service_port)
                service_env = {**env, "PORT": str(service_port), "BASE_PATH": "/", "PUBLIC_URL": "/"}
                try:
                    service_process = await asyncio.create_subprocess_exec("/bin/sh", "-c", spec["command"], cwd=root, env=service_env,
                        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT, preexec_fn=lambda: _workspace_user(uid), start_new_session=True)
                except BaseException:
                    _reserved_ports.discard(service_port)
                    raise
                service = Service(spec["name"], service_port, spec["command"], service_process)
                started_services.append(service)
                asyncio.create_task(_capture_output(service))
                await _wait_ready(service_process, service_port, spec.get("ready_path", "/"), lambda: service.output)
            process = await asyncio.create_subprocess_exec("/bin/sh", "-c", command, cwd=root, env=env,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, preexec_fn=lambda: _workspace_user(uid), start_new_session=True)
        except BaseException:
            for service in reversed(started_services):
                await _terminate(service.process)
                _reserved_ports.discard(service.port)
            _reserved_ports.discard(port)
            raise
        runtime = Runtime(project_id, token, port, command, base_aware, process,
                          services=started_services, configuration=configuration, source_version=source_version)
        _runtimes[project_id] = runtime
        _tokens[token] = runtime
        asyncio.create_task(_capture_output(runtime))
        path = prefix + "/" if base_aware else "/"
        try:
            await _wait_ready(process, port, path, lambda: runtime.output)
            runtime.ready = True
            return runtime
        except BaseException:
            await stop_runtime(project_id)
            raise


def runtime_by_token(token: str):
    runtime = _tokens.get(token)
    return runtime if runtime and runtime.ready and runtime.process.returncode is None and all(service.process.returncode is None for service in runtime.services) else None


def runtime_status(project_id: uuid.UUID):
    runtime = _runtimes.get(project_id)
    if not runtime:
        return None
    return {"running": runtime_by_token(runtime.token) is not None,
            "starting": not runtime.ready and runtime.process.returncode is None, "command": runtime.command,
            "output": runtime.output[-4000:] + "".join(f"\n[{service.name}]\n{service.output[-2000:]}" for service in runtime.services),
            "url": runtime.prefix + "/", "services": [{"name": service.name, "running": service.process.returncode is None} for service in runtime.services]}


def mark_runtime_limit(project_id: uuid.UUID, message: str):
    runtime = _runtimes.get(project_id)
    if runtime:
        runtime.output = (runtime.output + "\n" + message)[-16_000:]


def upstream_path(runtime: Runtime, path: str):
    return f"{runtime.prefix}/{path}" if runtime.base_aware else f"/{path}"
