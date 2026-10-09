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
_restore_tasks: set[asyncio.Task] = set()


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
    restoration_id: uuid.UUID | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    owned_project(PROJECT_ID)
    from project_restoration import recover_workspace
    with connection() as conn:
        completed = conn.execute("SELECT 1 FROM project_restores WHERE project_id=%s AND status='succeeded'", (PROJECT_ID,)).fetchone()
    backup_path = ensure_workspace(PROJECT_ID) / '.atoms-snapshots/active-restore'
    if completed and backup_path.exists():
        import shutil
        shutil.rmtree(backup_path)
    if (backup_path / 'manifest.json').exists():
        import json
        manifest = json.loads((backup_path / 'manifest.json').read_text())
        previous_data = manifest.get('runtime_state',{}).get('data')
        if previous_data:
            from project_data_client import checkpoint
            await checkpoint(PROJECT_ID,'restore',previous_data['id'])
    recovered = recover_workspace(ensure_workspace(PROJECT_ID))
    with connection() as conn:
        interrupted = conn.execute("SELECT status,result FROM project_restores WHERE project_id=%s AND status IN ('running','failed')", (PROJECT_ID,)).fetchone()
        if interrupted and (interrupted['status'] == 'running' or recovered):
            previous = recovered['project_status'] if recovered else interrupted['result'].get('previous_status', 'error')
            conn.execute("UPDATE projects SET status=%s,updated_at=NOW() WHERE id=%s AND status='restoring'", (previous, PROJECT_ID))
            conn.execute("UPDATE project_restores SET status='failed',error='还原过程被中断，已恢复操作前的工作区，请重试',updated_at=NOW() WHERE project_id=%s", (PROJECT_ID,))
            from restoration_messages import update_message
            update_message(conn,PROJECT_ID,status='failed',error='还原过程被中断，已恢复操作前的代码和数据，请重试')
    await resume_jobs(PROJECT_ID)
    monitor = asyncio.create_task(monitor_resources())
    try:
        yield
    finally:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
        await stop_all_runtimes()


@asynccontextmanager
async def restore_guard(project_id):
    # Drain writes accepted before status='restoring'. Polling the lock avoids
    # blocking the worker event loop while the initiating HTTP request returns.
    with connection() as conn:
        locked = False
        try:
            while not locked:
                locked = conn.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,19)) AS acquired', (str(project_id),)).fetchone()['acquired']
                conn.commit()
                if not locked:
                    await asyncio.sleep(0.1)
            yield
        finally:
            if locked:
                conn.execute('SELECT pg_advisory_unlock(hashtextextended(%s,19))', (str(project_id),))


async def perform_restore(project_id, version):
    def progress(phase):
        with connection() as conn:
            conn.execute('UPDATE project_restores SET phase=%s,updated_at=NOW() WHERE project_id=%s', (phase, project_id))
            from restoration_messages import update_message
            update_message(conn,project_id,phase=phase)
    async with project_lock(project_id):
        try:
            async with restore_guard(project_id):
                await restore_version(project_id, version, progress)
        except BaseException as exc:
            with connection() as conn:
                row = conn.execute('SELECT result FROM project_restores WHERE project_id=%s', (project_id,)).fetchone()
                previous = row['result'].get('previous_status', 'error')
                # If rollback failed, leave the workspace blocked until startup
                # recovers the retained checkpoint instead of allowing edits.
                backup = ensure_workspace(project_id) / '.atoms-snapshots/active-restore'
                conn.execute("UPDATE projects SET status=%s,updated_at=NOW() WHERE id=%s", ('restoring' if backup.exists() else previous, project_id))
                detail = (str(exc) or '还原被中断') + ('；检查点已保留，工作区等待恢复' if backup.exists() else '；已恢复操作前的工作区')
                conn.execute("UPDATE project_restores SET status='failed',error=%s,updated_at=NOW() WHERE project_id=%s", (detail[:3000], project_id))
                from restoration_messages import update_message
                update_message(conn,project_id,status='failed',error=detail[:3000])
            if isinstance(exc, asyncio.CancelledError):
                raise


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
        if project['status'] == 'restoring':
            raise HTTPException(409, '正在还原版本，请等待完成')
        await stop_runtime(project_id)
        return {"ok": True}
    if operation == "runtime_start":
        if project['status'] == 'restoring':
            raise HTTPException(409, '正在还原版本，请等待完成')
        try:
            runtime = await start_runtime(project_id, data.command, restart=data.restart)
        except RuntimeError as exc:
            raise HTTPException(502, str(exc)) from exc
        if not runtime:
            return {"mode": "none", "url": "", "command": "", "output": "没有检测到开发服务。请配置启动命令。"}
        return {"mode": "live", "url": runtime.prefix + "/", "command": runtime.command, "output": runtime.output[-2000:]}
    if operation not in {"command", "build", "restore"}:
        raise HTTPException(404, "Unknown operation")
    if project["status"] in ("running", "queued", "restoring") or project_lock(project_id).locked():
        raise HTTPException(409, "请等待当前任务结束")
    if operation == 'restore':
        backup = ensure_workspace(project_id) / '.atoms-snapshots/active-restore'
        if backup.exists():
            with connection() as conn:
                completed = conn.execute("SELECT 1 FROM project_restores WHERE project_id=%s AND status='succeeded'", (project_id,)).fetchone()
            if not completed:
                raise HTTPException(409, '存在未恢复的还原检查点，请等待工作区恢复')
            import shutil
            try:
                shutil.rmtree(backup)
            except OSError as exc:
                raise HTTPException(503, '还原检查点清理失败，请重启工作区后重试') from exc
        if data.version is None:
            raise HTTPException(422, 'Missing version')
        with connection() as conn:
            current = conn.execute('SELECT status FROM projects WHERE id=%s FOR UPDATE', (project_id,)).fetchone()
            if current['status'] in {'running', 'queued', 'restoring'}:
                raise HTTPException(409, '请等待当前任务结束')
            selected = conn.execute('SELECT runtime_state FROM project_versions WHERE project_id=%s AND version=%s', (project_id, data.version)).fetchone()
            if not selected:
                raise HTTPException(404, '版本不存在')
            if not selected['runtime_state'].get('data'):
                raise HTTPException(422,'此旧版本没有数据库和浏览器数据快照，无法完整还原；当前代码和数据未修改')
            operation_id = data.restoration_id or uuid.uuid4()
            conn.execute("INSERT INTO project_restores(project_id,id,version,status,phase,result) VALUES(%s,%s,%s,'running','正在校验版本文件',%s) ON CONFLICT(project_id) DO UPDATE SET id=EXCLUDED.id,version=EXCLUDED.version,status=EXCLUDED.status,phase=EXCLUDED.phase,error='',result=EXCLUDED.result,updated_at=NOW()",
                         (project_id, operation_id, data.version, Jsonb({'previous_status': current['status']})))
            conn.execute("UPDATE projects SET status='restoring',updated_at=NOW() WHERE id=%s", (project_id,))
            from restoration_messages import create_messages
            create_messages(conn,project_id,operation_id,data.version)
        task = asyncio.create_task(perform_restore(project_id, data.version))
        _restore_tasks.add(task)
        task.add_done_callback(_restore_tasks.discard)
        return {'ok': True, 'restore_id': str(operation_id)}
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
        code, output = await run_build(project_id)
        with connection() as conn:
            conn.execute("INSERT INTO project_commands(project_id,command,exit_code,output) VALUES(%s,%s,%s,%s)",
                         (project_id, "project build", code, output))
        if code:
            raise HTTPException(422, output[-3000:])
        root = ensure_workspace(project_id)
        preview = preview_document(root)
        from project_restoration import capture_version_state
        with connection() as conn:
            command = conn.execute('SELECT dev_command FROM projects WHERE id=%s',(project_id,)).fetchone()['dev_command']
        state = await capture_version_state(project_id,root,command)
        with connection() as conn:
            version = conn.execute("SELECT COALESCE(MAX(version),0)+1 AS n FROM project_versions WHERE project_id=%s", (project_id,)).fetchone()["n"]
            conn.execute("INSERT INTO project_versions(project_id,version,summary,files,preview_html,runtime_state) VALUES(%s,%s,%s,%s,%s,%s)",
                         (project_id, version, "手动编辑并构建", Jsonb(snapshot_files(root)), preview, Jsonb(state)))
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
