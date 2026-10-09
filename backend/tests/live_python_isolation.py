"""Opt-in real scheduler/container/Python isolation probe; no model or PyPI calls.

Run inside agent-service: PYTHONPATH=/app python tests/live_python_isolation.py
Creates two temporary projects, exercises them, then removes their resources.
"""
import asyncio
import json
from pathlib import Path
import shutil
import uuid

import httpx
from psycopg import sql

from agent_service import docker, worker_name
from main import connection, grant_project_role, project_db_role, AGENT_SECRET
from project_database import remove as remove_database
from test_project_python import wheel


async def main():
    owner = uuid.uuid4()
    projects = [uuid.uuid4(), uuid.uuid4()]
    headers = {'X-Agent-Secret': AGENT_SECRET}
    endpoint = 'http://127.0.0.1:9001'
    created = []
    evidence = []
    with connection() as conn:
        conn.execute('INSERT INTO users(id,email,password_hash) VALUES(%s,%s,%s)',
                     (owner, f'python-isolation-{owner.hex}@invalid.example', uuid.uuid4().hex))
    async with httpx.AsyncClient(timeout=240) as client:
        async def invoke(project, operation, **payload):
            response = await client.post(f'{endpoint}/projects/{project}/invoke/{operation}', headers=headers, json=payload)
            response.raise_for_status()
            result = response.json()
            if result.get('exit_code', 0):
                raise RuntimeError(result.get('output'))
            return result
        try:
            for project, version in zip(projects, ('1.0.0', '2.0.0')):
                relative = f'projects/{project.hex}'
                root = Path('/workspaces') / relative
                root.mkdir(parents=True)
                created.append((project, root))
                (root / 'wheels').mkdir()
                wheel(root / 'wheels', version)
                (root / 'requirements.txt').write_text(f'--no-index\n--find-links ./wheels\nhttpx=={version}\n')
                # Reproduce the original bug: a root-owned install destination.
                (root / '.python-packages').mkdir()
                (root / 'server.py').write_text('''import http.server,json,os,sys,httpx
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(json.dumps({'version':httpx.__version__,'prefix':sys.prefix}).encode())
http.server.HTTPServer(('127.0.0.1',int(os.environ['PORT'])),Handler).serve_forever()
''')
                (root / '.atoms-workspace.json').write_text(json.dumps({'dev': 'python server.py'}))
                with connection() as conn:
                    conn.execute('INSERT INTO projects(id,owner_id,title,prompt,workspace_path) VALUES(%s,%s,%s,%s,%s)',
                                 (project, owner, 'Temporary Python isolation probe', 'No model calls', relative))
                    grant_project_role(conn, project)
                await invoke(project, 'build')
                result = await invoke(project, 'command', command='atoms-python-probe')
                assert result['output'].strip() == version, result
                result = await invoke(project, 'command', command="python -c 'import importlib.util; assert importlib.util.find_spec(\"fastapi\") is None; print(\"no-platform-packages\")'")
                assert 'no-platform-packages' in result['output'], result
                live = await invoke(project, 'runtime_start')
                response = await client.get(f'{endpoint}/projects/{project}/preview/{live["url"].strip("/").split("/")[-1]}/', headers=headers)
                response.raise_for_status()
                assert response.json() == {'version': version, 'prefix': f'/workspaces/{project.hex}/.venv'}, response.text
                container = (await docker('GET', f'/containers/{worker_name(project)}/json')).json()
                mounts = [mount for mount in container['Mounts'] if mount['RW']]
                assert len(mounts) == 1 and mounts[0]['Destination'] == f'/workspaces/{project.hex}'
                assert container['HostConfig']['ReadonlyRootfs']
                assert container['HostConfig']['NetworkMode'] == worker_name(project)
                evidence.append({'project': project.hex, 'version': version, 'private_interpreter': response.json()['prefix'],
                                 'container': container['Id'][:12], 'workspace_mount': mounts[0]['Source'],
                                 'network': container['HostConfig']['NetworkMode'], 'readonly_rootfs': True})
            first, second = projects
            await invoke(first, 'command', command="python -c 'from pathlib import Path; Path(\".python-packages/httpx/__init__.py\").write_text(\"__version__=\\\"changed-in-A\\\"\\n\")'")
            result = await invoke(second, 'command', command='atoms-python-probe')
            assert result['output'].strip() == '2.0.0', result
            await invoke(second, 'runtime_stop')
            live = await invoke(second, 'runtime_start')
            response = await client.get(f'{endpoint}/projects/{second}/preview/{live["url"].strip("/").split("/")[-1]}/', headers=headers)
            response.raise_for_status()
            assert response.json()['version'] == '2.0.0'
            print(json.dumps({'ok': True, 'cross_project_mutation_isolated': True,
                              'runtime_restart_preserved_dependencies': True, 'projects': evidence}, indent=2))
        finally:
            for project, root in created:
                response = await client.delete(f'{endpoint}/projects/{project}', headers=headers)
                response.raise_for_status()
                with connection() as conn:
                    remove_database(conn, project)
                    role = project_db_role(project)
                    if conn.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (role,)).fetchone():
                        conn.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename=%s', (role,))
                        conn.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
                        conn.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
                    conn.execute('DELETE FROM projects WHERE id=%s', (project,))
                shutil.rmtree(root)
            with connection() as conn:
                conn.execute('DELETE FROM users WHERE id=%s', (owner,))


if __name__ == '__main__':
    asyncio.run(main())
