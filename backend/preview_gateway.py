"""Restricted, separate-origin entry point for project previews."""

import hashlib
import json
import re

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from psycopg.types.json import Jsonb

from main import app as gateway_app, connection, VISUAL_EDITOR_SCRIPT


from browser_storage import STORAGE_SHIM
from preview_diagnostics import PREVIEW_DIAGNOSTICS, inject_preview_diagnostics, remove_preview_diagnostics


def project_cors_headers(scope):
    """Allow project-defined headers without granting platform credentials.

    Preview documents have an opaque sandbox origin, so even same-host fetch
    needs preflight. Authentication header names belong to the application.
    Cookies/control secrets remain stripped by the forwarding layer.
    """
    names = ['Authorization', 'Content-Type', 'Accept']
    requested = dict(scope.get('headers', [])).get(b'access-control-request-headers', b'').decode('latin-1')
    blocked = {'cookie', 'host', 'origin', 'connection', 'x-agent-secret', 'x-worker-token'}
    if len(requested) <= 4096:
        for name in requested.split(',')[:64]:
            name = name.strip()
            if (len(name) <= 128 and re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name)
                    and name.lower() not in blocked and name.lower() not in {n.lower() for n in names}):
                names.append(name)
    return ', '.join(names)


class PreviewHeaders:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        preview_request = scope["path"].startswith(("/api/runtime/", "/api/preview/", "/api/storage/"))
        if preview_request:
            scope["atoms_project_preview"] = True
            if scope["method"] == "OPTIONS":
                await Response(status_code=204, headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS, HEAD",
                    "Access-Control-Allow-Headers": project_cors_headers(scope),
                    "Access-Control-Max-Age": "600",
                    "Vary": "Access-Control-Request-Headers",
                })(scope, receive, send)
                return

        start_message = None
        html_parts = []
        is_html = False

        async def secure_send(message):
            nonlocal start_message, is_html
            if message["type"] == "http.response.start":
                is_html = any(name.lower() == b"content-type" and b"text/html" in value.lower()
                              for name, value in message.get("headers", []))
                headers = [(name, value) for name, value in message.get("headers", [])
                           if name.lower() != b"set-cookie" and
                           (not preview_request or not name.lower().startswith(b"access-control-")) and
                           (not is_html or name.lower() not in (b"content-length", b"content-encoding"))]
                if preview_request:
                    headers.extend([(b"access-control-allow-origin", b"*"),
                                    (b"access-control-allow-methods", b"GET, POST, PUT, PATCH, DELETE, OPTIONS, HEAD"),
                                    (b"access-control-allow-headers", project_cors_headers(scope).encode('latin-1')),
                                    (b"cache-control", b"no-store")])
                message["headers"] = headers
                if is_html:
                    start_message = message
                else:
                    await send(message)
            elif message["type"] == "http.response.body":
                if is_html:
                    html_parts.append(message.get("body", b""))
                else:
                    await send(message)
            else:
                await send(message)

        await self.app(scope, receive, secure_send)
        if start_message is not None:
            html = b"".join(html_parts).decode("utf-8", errors="replace")
            html = re.sub(r'<script data-atoms-visual-editor>.*?</script>', '', html, flags=re.DOTALL)
            html = remove_preview_diagnostics(html)
            preview_scripts = STORAGE_SHIM + PREVIEW_DIAGNOSTICS + VISUAL_EDITOR_SCRIPT
            html = inject_preview_diagnostics(html, preview_scripts)
            await send(start_message)
            await send({"type": "http.response.body", "body": html.encode("utf-8"), "more_body": False})


app = FastAPI(title="Atoms Preview Gateway", docs_url=None, redoc_url=None, openapi_url=None)
app.router.routes.extend(route for route in gateway_app.router.routes
                         if getattr(route, "path", "").startswith(("/api/runtime/", "/api/preview/")))
app.add_middleware(PreviewHeaders)


def project_for_token(token: str):
    if not 20 <= len(token) <= 128 or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        raise HTTPException(404, "预览链接已失效")
    digest = hashlib.sha256(token.encode()).hexdigest()
    with connection() as conn:
        row = conn.execute("""
            SELECT project_id FROM runtime_routes WHERE token_hash=%s
            UNION ALL
            SELECT project_id FROM preview_tokens WHERE token_hash=%s AND expires_at>NOW()
            LIMIT 1
        """, (digest, digest)).fetchone()
    if not row:
        raise HTTPException(404, "预览链接已失效")
    return row["project_id"]


@app.api_route("/api/storage/{token}", methods=["GET", "POST", "OPTIONS"])
async def storage(token: str, request: Request):
    if request.method == "OPTIONS":
        return Response(status_code=200)
    project_id = project_for_token(token)
    if request.method == "GET":
        session = request.query_params.get('session','')
        if session and not re.fullmatch(r'[A-Za-z0-9_-]{8,128}',session):
            raise HTTPException(422,'预览会话标识无效')
        with connection() as conn:
            conn.execute('INSERT INTO preview_storage(project_id) VALUES(%s) ON CONFLICT DO NOTHING',(project_id,))
            row = conn.execute("SELECT data,sessions,epoch FROM preview_storage WHERE project_id=%s", (project_id,)).fetchone()
        return JSONResponse({"data":row['data'] if row else {},'session':row['sessions'].get(session,{}) if row else {},'epoch':str(row['epoch']) if row else None})
    body = await request.body()
    if len(body) > 1_100_000:
        raise HTTPException(413, "预览存储写入过大")
    try:
        update = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(422, "预览存储请求无效") from exc
    operation = update.get("operation") if isinstance(update, dict) else None
    if operation not in ("set", "remove", "clear"):
        raise HTTPException(422, "预览存储操作无效")
    key = update.get("key") if isinstance(update, dict) else None
    if operation != "clear" and (not isinstance(key, str) or len(key.encode()) > 512):
        raise HTTPException(422, "预览存储键无效")
    value = update.get("value") if isinstance(update, dict) else None
    if operation == "set" and (not isinstance(value, str) or len(value.encode()) > 1_000_000):
        raise HTTPException(413, "预览存储值过大")
    with connection() as conn:
        conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,31))',(str(project_id),))
        if not conn.execute('SELECT pg_try_advisory_xact_lock(hashtextextended(%s,19)) AS acquired',(str(project_id),)).fetchone()['acquired']:
            raise HTTPException(409,'正在还原，请等待完成后再修改浏览器数据')
        project = conn.execute('SELECT status FROM projects WHERE id=%s FOR SHARE',(project_id,)).fetchone()
        if project['status'] in {'restoring','deleting'}:
            raise HTTPException(409,'正在还原，请等待完成后再修改浏览器数据')
        conn.execute("INSERT INTO preview_storage(project_id) VALUES(%s) ON CONFLICT DO NOTHING", (project_id,))
        row = conn.execute("SELECT data,sessions,epoch FROM preview_storage WHERE project_id=%s FOR UPDATE", (project_id,)).fetchone()
        if update.get('epoch')!=str(row['epoch']):
            raise HTTPException(409,'预览版本已切换，请重新加载应用后再操作')
        area = update.get('area','local')
        session = update.get('session','')
        if area not in {'local','session'} or (area=='session' and (not isinstance(session,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}',session))):
            raise HTTPException(422,'预览存储区域或会话标识无效')
        sessions=dict(row['sessions'])
        data = dict(row['data'] if area=='local' else sessions.get(session,{}))
        if operation == "set":
            data[key] = value
        elif operation == "remove":
            data.pop(key, None)
        else:
            data.clear()
        if len(json.dumps(data, ensure_ascii=False).encode()) > 2_000_000:
            raise HTTPException(413, "预览存储总量超过 2 MB")
        if area=='local':
            conn.execute("UPDATE preview_storage SET data=%s,updated_at=NOW() WHERE project_id=%s",(Jsonb(data),project_id))
        else:
            sessions[session]=data
            if len(json.dumps(sessions,ensure_ascii=False).encode())>4_000_000:
                raise HTTPException(413,'预览会话存储总量超过 4 MB')
            conn.execute("UPDATE preview_storage SET sessions=%s,updated_at=NOW() WHERE project_id=%s",(Jsonb(sessions),project_id))
    return {'ok':True,'epoch':str(row['epoch'])}


@app.get("/health")
def health():
    return {"ok": True}
