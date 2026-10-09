"""Opt-in HTTP -> frontend proxy -> API -> Worker probe; no model calls."""
import asyncio
import json
from pathlib import Path
import shutil
import tempfile
import uuid
import zipfile

from fastapi import Response
import httpx
from psycopg import sql

from auth import issue_session
from main import connection, grant_project_role, project_db_role, AGENT_SECRET
from project_database import remove as remove_database
from project_snapshots import file_identity


async def main():
    owner, project = uuid.uuid4(), uuid.uuid4()
    root = Path('/workspaces/projects') / project.hex
    root.mkdir(parents=True)
    internal = {'X-Agent-Secret': AGENT_SECRET}
    size = 128 * 1024 * 1024
    with connection() as conn:
        conn.execute('INSERT INTO users(id,email,password_hash) VALUES(%s,%s,%s)',
                     (owner, f'large-project-{owner.hex}@invalid.example', uuid.uuid4().hex))
        conn.execute('INSERT INTO projects(id,owner_id,title,prompt,workspace_path) VALUES(%s,%s,%s,%s,%s)',
                     (project, owner, 'Temporary large-project probe', 'No model calls', f'projects/{project.hex}'))
        grant_project_role(conn, project)
        token = issue_session(conn, owner, Response())['access_token']
    # Match the public IP Host used by the browser; Vite rejects internal
    # Docker service names through its independent host validation.
    headers = {'Authorization': 'Bearer ' + token, 'Host': '120.55.37.39:25173'}
    (root / '.atoms-workspace.json').write_text(json.dumps({
        'build': 'python -c \'print("large-project build")\'',
        'dev': 'python -m http.server $PORT --bind 127.0.0.1'}))
    (root / 'index.html').write_text('<!doctype html><title>Large project probe</title><h1>Ready</h1>')
    endpoint = f'http://frontend:5173/api/projects/{project}'
    async with httpx.AsyncClient(timeout=240) as client:
        try:
            async def upload_body():
                for _ in range(128):
                    yield b'\0\xff' * (512 * 1024)
            response = await client.put(endpoint + '/assets/large.bin',
                headers={**headers, 'Content-Length': str(size)}, content=upload_body())
            response.raise_for_status()
            assert response.json()['size'] == size, response.text
            asset_path = response.json()['path']
            response = await client.post(endpoint + '/build', headers=headers, json={})
            response.raise_for_status()
            version = response.json()['version']
            with connection() as conn:
                files = conn.execute('SELECT files FROM project_versions WHERE project_id=%s AND version=%s', (project, version)).fetchone()['files']
            saved = files[asset_path]
            assert saved['encoding'] == 'blob' and saved['size'] == size
            manifest_bytes = len(json.dumps(files).encode())
            assert manifest_bytes < 1024 * 1024
            response = await client.put(endpoint + '/assets/large.bin', headers=headers, content=b'changed')
            response.raise_for_status()
            response = await client.post(endpoint + f'/restore/{version}', headers=headers, json={})
            assert response.is_success, response.text
            response.raise_for_status()
            assert 'restore_preview_error' not in response.json(), response.text
            assert file_identity(root / asset_path) == saved
            with tempfile.TemporaryFile() as downloaded:
                async with client.stream('GET', endpoint + '/archive', headers=headers) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes():
                        downloaded.write(chunk)
                with zipfile.ZipFile(downloaded) as archive:
                    assert archive.getinfo(asset_path).file_size == size
                    assert not any('.atoms-snapshots' in name for name in archive.namelist())
            content = '// 中文源码\n' * 30000
            response = await client.put(endpoint + '/files/large.ts', headers=headers, json={'content': content})
            response.raise_for_status()
            response = await client.get(endpoint + '/files/large.ts', headers=headers)
            response.raise_for_status()
            assert response.json()['content'] == content
            response = await client.post(endpoint + '/clone', headers=headers,
                                         json={'title': 'Temporary large-project clone', 'copy_database': False})
            response.raise_for_status()
            clone = uuid.UUID(response.json()['id'])
            with connection() as conn:
                clone_workspace = conn.execute('SELECT workspace_path FROM projects WHERE id=%s', (clone,)).fetchone()['workspace_path']
            assert file_identity(Path('/workspaces') / clone_workspace / asset_path) == saved
            print(json.dumps({'ok': True, 'uploaded_bytes_through_frontend_proxy': size, 'saved_version': version,
                              'database_manifest_bytes': manifest_bytes, 'restore_hash_verified': True,
                              'source_archive_verified': True, 'large_text_edit_bytes': len(content.encode()),
                              'clone_verified': True, 'no_model_calls': True}, indent=2), flush=True)
        finally:
            # Restrict all cleanup to this randomly generated owner and its probes.
            with connection() as conn:
                probes = conn.execute('SELECT id,workspace_path FROM projects WHERE owner_id=%s', (owner,)).fetchall()
            for row in probes:
                current = row['id']
                response = await client.delete(f'http://127.0.0.1:9001/projects/{current}', headers=internal)
                response.raise_for_status()
                with connection() as conn:
                    remove_database(conn, current)
                    role = project_db_role(current)
                    if conn.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (role,)).fetchone():
                        conn.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename=%s', (role,))
                        conn.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
                        conn.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
                    conn.execute('DELETE FROM projects WHERE id=%s', (current,))
                shutil.rmtree(Path('/workspaces') / row['workspace_path'])
            with connection() as conn:
                conn.execute('DELETE FROM users WHERE id=%s', (owner,))


if __name__ == '__main__':
    asyncio.run(main())
