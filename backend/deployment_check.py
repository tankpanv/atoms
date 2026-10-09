"""Live deployment probes, not mocked tests. Run inside the relevant service container."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
import urllib.request


def get(url):
    with urllib.request.urlopen(url, timeout=15) as response:
        return json.load(response)


async def worker_environment():
    from agent_service import docker, IMAGE, VOLUME, COMPOSE_PROJECT
    for path in ['/_ping', f'/images/{IMAGE}/json', f'/volumes/{VOLUME}', f'/networks/{COMPOSE_PROJECT}_default']:
        response = await docker('GET', path)
        if response.status_code != 200:
            raise RuntimeError(f'Worker 环境不可用: {path} (HTTP {response.status_code})')
    print('PASS Agent Docker socket / Worker 镜像 / 工作区卷 / 项目网络')


async def browser():
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        try:
            page = await browser.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            response = await page.goto('http://frontend:5173/zh/dashboard', wait_until='networkidle', timeout=45000)
            if not response or response.status != 200:
                raise RuntimeError('浏览器首页 HTTP 未成功')
            await page.locator('#root > *').first.wait_for(timeout=15000)
            if not (await page.locator('body').inner_text()).strip():
                raise RuntimeError('浏览器首屏为空')
            if errors:
                raise RuntimeError('浏览器运行错误: ' + errors[0][:300])
        finally:
            await browser.close()
    print('PASS Chromium 真实加载前端并渲染，无未捕获运行错误')


async def project_runtime():
    """Exercise the actual scheduler -> isolated worker -> preview route, then clean up."""
    import hashlib
    import hmac
    from pathlib import Path
    import shutil
    import httpx
    from psycopg import sql
    from main import connection, grant_project_role, project_db_role, AGENT_SECRET, AGENT_SERVICES
    from project_database import remove as remove_database

    project_id, owner_id = uuid.uuid4(), uuid.uuid4()
    relative = f'projects/{project_id.hex}'
    workspace = Path('/workspaces') / relative
    marker = f'atoms-deployment-{project_id.hex}'
    endpoint = AGENT_SERVICES[0]
    headers = {'X-Agent-Secret': AGENT_SECRET}
    workspace.mkdir(parents=True)
    try:
        (workspace / 'index.html').write_text(f'<!doctype html><html><body><h1>{marker}</h1></body></html>')
        with connection() as conn:
            conn.execute('INSERT INTO users(id,email,password_hash) VALUES(%s,%s,%s)',
                         (owner_id, f'deployment-{owner_id.hex}@invalid.example', uuid.uuid4().hex))
            conn.execute("INSERT INTO projects(id,owner_id,title,prompt,workspace_path) VALUES(%s,%s,%s,%s,%s)",
                         (project_id, owner_id, '临时部署链路检查', 'deployment probe; no model calls', relative))
            grant_project_role(conn, project_id)
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(f'{endpoint}/projects/{project_id}/invoke/runtime_start',
                headers=headers, json={'command': 'python -m http.server $PORT --bind 127.0.0.1'})
            response.raise_for_status()
            prefix = response.json().get('url', '')
            if not prefix.startswith('/api/runtime/'):
                raise RuntimeError('Worker 未返回真实运行服务')
            token = prefix.strip('/').split('/')[-1]
            worker_token = hmac.new(AGENT_SECRET.encode(), b'worker-project:' + project_id.bytes, hashlib.sha256).hexdigest()
            response = await client.post(f'{endpoint}/projects/{project_id}/preview-check/{token}',
                                         headers={'X-Worker-Token': worker_token})
            response.raise_for_status()
            response = await client.get('http://preview:8002' + prefix)
            response.raise_for_status()
            if marker not in response.text:
                raise RuntimeError('预览网关未返回当前 Worker 的真实页面')
        print('PASS 真实调度 -> 隔离 Worker 启动 -> 项目 HTTP 服务 -> 预览网关回读（无模型调用）')
    finally:
        # Delete only this random probe project; never touch user workspaces.
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.delete(f'{endpoint}/projects/{project_id}', headers=headers)
            response.raise_for_status()
        with connection() as conn:
            remove_database(conn, project_id)
            role = project_db_role(project_id)
            if conn.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (role,)).fetchone():
                conn.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename=%s', (role,))
                conn.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
                conn.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
            conn.execute('DELETE FROM projects WHERE id=%s', (project_id,))
            conn.execute('DELETE FROM users WHERE id=%s', (owner_id,))
        shutil.rmtree(workspace)


def main():
    if '--worker-environment' in sys.argv:
        asyncio.run(worker_environment())
        return
    import psycopg
    with psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=10) as connection:
        assert connection.execute('SELECT 1').fetchone()[0] == 1
        for table in ['users', 'projects', 'auth_sessions', 'agent_jobs']:
            if not connection.execute('SELECT to_regclass(%s)', (table,)).fetchone()[0]:
                raise RuntimeError(f'数据库缺少表 {table}')
    print('PASS PostgreSQL 连接与自动建表')
    from published_storage import ensure_bucket, client, BUCKET
    ensure_bucket()
    key = f'_deployment_checks/{uuid.uuid4().hex}'
    payload = b'atoms-live-storage-check'
    storage = client()
    try:
        storage.put_object(Bucket=BUCKET, Key=key, Body=payload)
        result = storage.get_object(Bucket=BUCKET, Key=key)
        try:
            assert result['Body'].read() == payload
        finally:
            result['Body'].close()
    finally:
        storage.delete_object(Bucket=BUCKET, Key=key)
    print('PASS MinIO bucket 创建 / 写入 / 读取 / 删除')
    assert get('http://frontend:5173/api/health')['database'] == 'connected'
    assert get('http://frontend:5173/api/auth/session')['authenticated'] is False
    assert 'BEGIN PUBLIC KEY' in get('http://frontend:5173/api/auth/public-key')['public_key']
    assert get('http://preview:8002/health')['ok'] is True
    for service in os.environ.get('AGENT_SERVICES', 'http://agent-service:9001').split(','):
        assert get(service.strip().rstrip('/') + '/health')['role'] == 'agent-service'
    print('PASS 前端 API 代理 / 登录公钥 / 预览网关 / Agent HTTP')
    asyncio.run(browser())
    if '--runtime' in sys.argv:
        asyncio.run(project_runtime())


if __name__ == '__main__':
    main()
