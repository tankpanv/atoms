"""Isolated agent loop and development server for one project."""

from __future__ import annotations

import hmac
import asyncio
import os
import signal
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket
from pydantic import BaseModel
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agent import ensure_workspace, preview_document, project_uid, run_build, run_shell, snapshot_files
from jobs import project_lock, restore_version, resume_jobs, schedule_project
from runtime import mark_runtime_limit, runtime_by_token, runtime_status, start_runtime, stop_all_runtimes, stop_runtime


USER_ID = uuid.UUID(os.environ["USER_ID"])
PROJECT_ID = uuid.UUID(os.environ["PROJECT_ID"])
WORKER_TOKEN = os.environ["WORKER_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
PROJECT_RSS_LIMIT = int(os.getenv("PROJECT_RSS_LIMIT_MB", "1536")) * 1024 * 1024
_project_uids: dict[int, uuid.UUID] = {project_uid(PROJECT_ID): PROJECT_ID}


def connection():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def authorize(x_worker_token: str = Header(default="")):
    if not hmac.compare_digest(x_worker_token, WORKER_TOKEN):
        raise HTTPException(403, "Invalid worker token")


def owned_project(project_id: uuid.UUID):
    if project_id != PROJECT_ID:
        raise HTTPException(404, "Project not found in this worker")
    with connection() as conn:
        row = conn.execute("SELECT owner_id,status FROM projects WHERE id=%s", (project_id,)).fetchone()
    if not row or row["owner_id"] != USER_ID:
        raise HTTPException(404, "Project not found in this worker")
    if row["status"] == "deleting":
        raise HTTPException(409, "Project is being deleted")
    return row


async def monitor_resources():
    page_size = os.sysconf("SC_PAGE_SIZE")
    while True:
        await asyncio.sleep(1)
        usage: dict[int, int] = {}
        processes: dict[int, list[int]] = {}
        for entry in os.scandir("/proc"):
            if not entry.name.isdecimal():
                continue
            try:
                uid = entry.stat().st_uid
                if uid not in _project_uids:
                    continue
                fields = (Path(entry.path) / "statm").read_text().split()
                usage[uid] = usage.get(uid, 0) + int(fields[1]) * page_size
                processes.setdefault(uid, []).append(int(entry.name))
            except (FileNotFoundError, PermissionError, ValueError, IndexError):
                continue
        for uid, used in usage.items():
            if used <= PROJECT_RSS_LIMIT:
                continue
            project_id = _project_uids[uid]
            mark_runtime_limit(project_id, "项目进程超过内存配额，已停止。")
            for process_id in processes.get(uid, []):
                try:
                    os.kill(process_id, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            try:
                with connection() as conn:
                    for job in conn.execute("SELECT id FROM agent_jobs WHERE project_id=%s AND status='running'", (project_id,)).fetchall():
                        conn.execute("INSERT INTO agent_steps(job_id,kind,label,detail) VALUES(%s,'warning','项目资源配额已触发',%s)",
                                     (job["id"], f"项目进程内存 {used // (1024 * 1024)} MB，限制 {PROJECT_RSS_LIMIT // (1024 * 1024)} MB"))
            except psycopg.Error:
                pass


class InvokeRequest(BaseModel):
    command: str = ""
    restart: bool = False
    version: int | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    owned_project(PROJECT_ID)
    await resume_jobs(PROJECT_ID)
    monitor = asyncio.create_task(monitor_resources())
    try:
        yield
    finally:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
        await stop_all_runtimes()


app = FastAPI(title="Atoms Project Worker", lifespan=lifespan)


@app.get("/health")
def health():
    return {"ok": True, "user_id": str(USER_ID), "project_id": str(PROJECT_ID)}


@app.post("/projects/{project_id}/invoke/{operation}", dependencies=[Depends(authorize)])
async def invoke(project_id: uuid.UUID, operation: str, data: InvokeRequest):
    project = owned_project(project_id)
    if operation == "run":
        schedule_project(project_id)
        return {"ok": True}
    if operation == "runtime_status":
        return {"status": runtime_status(project_id)}
    if operation == "runtime_stop":
        await stop_runtime(project_id)
        return {"ok": True}
    if operation == "runtime_start":
        try:
            runtime = await start_runtime(project_id, data.command, restart=data.restart)
        except RuntimeError as exc:
            raise HTTPException(502, str(exc)) from exc
        if not runtime:
            return {"mode": "none", "url": "", "command": "", "output": "没有检测到开发服务。请配置启动命令。"}
        return {"mode": "live", "url": runtime.prefix + "/", "command": runtime.command, "output": runtime.output[-2000:]}
    if operation not in {"command", "build", "restore"}:
        raise HTTPException(404, "Unknown operation")
    if project["status"] in ("running", "queued") or project_lock(project_id).locked():
        raise HTTPException(409, "请等待当前任务结束")
    async with project_lock(project_id):
        if operation == "command":
            try:
                code, output = await run_shell(project_id, data.command)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            with connection() as conn:
                row = conn.execute("INSERT INTO project_commands(project_id,command,exit_code,output) VALUES(%s,%s,%s,%s) RETURNING *",
                                   (project_id, data.command, code, output)).fetchone()
            return {**row, "project_id": str(row["project_id"]), "created_at": row["created_at"].isoformat()}
        if operation == "restore":
            if data.version is None:
                raise HTTPException(422, "Missing version")
            try:
                restored = await restore_version(project_id, data.version)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            return {"ok": True, "version": restored}
        code, output = await run_build(project_id)
        with connection() as conn:
            conn.execute("INSERT INTO project_commands(project_id,command,exit_code,output) VALUES(%s,%s,%s,%s)",
                         (project_id, "project build", code, output))
        if code:
            raise HTTPException(422, output[-3000:])
        root = ensure_workspace(project_id)
        preview = preview_document(root)
        with connection() as conn:
            version = conn.execute("SELECT COALESCE(MAX(version),0)+1 AS n FROM project_versions WHERE project_id=%s", (project_id,)).fetchone()["n"]
            conn.execute("INSERT INTO project_versions(project_id,version,summary,files,preview_html) VALUES(%s,%s,%s,%s,%s)",
                         (project_id, version, "手动编辑并构建", Jsonb(snapshot_files(root)), preview))
            conn.execute("UPDATE projects SET preview_html=%s,status='ready',updated_at=NOW() WHERE id=%s", (preview, project_id))
        return {"ok": True, "version": version, "output": output[-3000:]}


@app.api_route("/projects/{project_id}/api/runtime/{token}/", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
@app.api_route("/projects/{project_id}/api/runtime/{token}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def preview(project_id: uuid.UUID, token: str, request: Request, path: str = ""):
    runtime = runtime_by_token(token)
    if not runtime or runtime.project_id != project_id:
        raise HTTPException(404, "Project runtime not found")
    from main import proxy_runtime
    return await proxy_runtime(token, request, path)


@app.websocket("/projects/{project_id}/api/runtime/{token}/")
@app.websocket("/projects/{project_id}/api/runtime/{token}/{path:path}")
async def preview_websocket(websocket: WebSocket, project_id: uuid.UUID, token: str, path: str = ""):
    runtime = runtime_by_token(token)
    if not runtime or runtime.project_id != project_id:
        await websocket.close(code=1008)
        return
    from main import proxy_runtime_websocket
    await proxy_runtime_websocket(websocket, token, path)
