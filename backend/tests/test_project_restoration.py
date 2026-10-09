"""Real file, PostgreSQL, process, and Chromium restoration regression tests."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
import subprocess
import sys
from unittest.mock import patch, AsyncMock

from psycopg.types.json import Jsonb

import agent
import jobs
import runtime
from agent_session import AgentSession, session_status
from project_restoration import (WorkspaceBackup, apply_runtime_state, capture_runtime_state,
                                 recover_workspace, restore_version, normalize_legacy_version)


class RuntimeCheckpointTests(unittest.TestCase):
    @unittest.skipUnless(os.geteuid() == 0, 'requires worker root and CAP_CHOWN')
    def test_project_owned_modes_restore_without_fowner_capability(self):
        # Reproduce the production worker's capability boundary in a child;
        # the parent process and other tests keep their original capabilities.
        code = '''
import ctypes, os, tempfile
from pathlib import Path
from project_restoration import apply_runtime_state
class Header(ctypes.Structure):
    _fields_=[('version',ctypes.c_uint32),('pid',ctypes.c_int)]
class Data(ctypes.Structure):
    _fields_=[('effective',ctypes.c_uint32),('permitted',ctypes.c_uint32),('inheritable',ctypes.c_uint32)]
with tempfile.TemporaryDirectory() as temporary:
    root=Path(temporary)
    path=root/'run.sh'
    path.write_text('echo restored')
    os.chown(path,12345,12345)
    libc=ctypes.CDLL(None,use_errno=True)
    header=Header(0x20080522,0)
    caps=(Data*2)()
    assert libc.capget(ctypes.byref(header),caps)==0
    caps[0].effective &= ~(1<<3)
    caps[0].permitted &= ~(1<<3)
    assert libc.capset(ctypes.byref(header),caps)==0
    try:
        path.chmod(0o755)
        raise AssertionError('Expected production chmod restriction')
    except PermissionError:
        pass
    apply_runtime_state(root,{'modes':{'run.sh':0o755}})
    assert path.stat().st_mode & 0o777 == 0o755
    assert path.stat().st_uid == 12345
    assert path.stat().st_gid == 12345
'''
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
    def test_legacy_cache_filter_rejects_traversal_and_retains_secrets_for_validation(self):
        for name in ('../.python-cache/state.json', '/.python-cache/state.json', '.python-cache/../app.py', '.python-cache\\state'):
            with self.assertRaises(ValueError):
                normalize_legacy_version({name: 'data'}, {})
        files, state = normalize_legacy_version({'app.py':'source', '.python-cache/state.json':'old cache', '.env':'secret'}, {'modes':{'.python-cache/state.json':420,'app.py':420}})
        self.assertEqual(files, {'app.py':'source','.env':'secret'})
        self.assertEqual(state['modes'], {'app.py':420})

    def test_session_snapshot_includes_wal_and_restores_completed_demo(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / 'run.sh'
            executable.write_text('#!/bin/sh\necho old\n')
            executable.chmod(0o755)
            session = AgentSession(root)
            session.state = {'messages': [{'role': 'user', 'content': 'old'}], 'completed': True,
                             'demo': {'ready': True, 'kind': 'web', 'report': 'old demo'}}
            session.save('fixture')
            original = capture_runtime_state(root, 'old command')
            session.state['demo']['kind'] = 'artifact'
            session.save('later failed task')
            executable.chmod(0o644)
            apply_runtime_state(root, original)
            self.assertEqual(executable.stat().st_mode & 0o777, 0o755)
            self.assertEqual(session_status(root)['demo']['kind'], 'web')
            self.assertTrue(session_status(root)['completed'])
            apply_runtime_state(root, {})
            self.assertFalse(session_status(root)['available'])

    def test_interrupted_detach_recovers_untouched_and_moved_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / uuid.uuid4().hex
            root.mkdir()
            (root / 'index.html').write_text('before')
            for name in ['node_modules', 'dist']:
                (root / name).mkdir()
                (root / name / 'original').write_text(name)
            with patch('agent.project_uid', return_value=os.getuid()):
                backup = WorkspaceBackup(root, {'status': 'error', 'dev_command': ''})
                # Simulate process death after the first rename.
                first = backup.state['paths'][0]
                target = backup.directory / 'generated' / first
                target.parent.mkdir(parents=True)
                (root / first).rename(target)
                (root / 'index.html').write_text('partially restored')
                recover_workspace(root)
            self.assertEqual((root / 'index.html').read_text(), 'before')
            for name in ['node_modules', 'dist']:
                self.assertEqual((root / name / 'original').read_text(), name)


class VerifiedRestoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.user, self.project = uuid.uuid4(), uuid.uuid4()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / self.project.hex
        self.root.mkdir()
        self.patches = [patch('agent.ensure_workspace', return_value=self.root),
                        patch('runtime.ensure_workspace', return_value=self.root),
                        patch('agent.project_uid', return_value=os.getuid()),
                        patch('runtime.project_uid', return_value=os.getuid()),
                        patch.dict(os.environ, {'WORKER_TOKEN': '', 'PREVIEW_GATEWAY_URL': ''}),
                        patch('project_data_client.checkpoint',AsyncMock(return_value={'format':1,'id':str(uuid.uuid4())}))]
        for item in self.patches:
            item.start()
        with jobs.connection() as conn:
            jobs.init_jobs_db(conn)
            conn.execute("INSERT INTO users(id,email,password_hash) VALUES(%s,%s,'test')", (self.user, f'restore-{self.user}@example.test'))
            conn.execute("INSERT INTO projects(id,owner_id,title,prompt,status) VALUES(%s,%s,'Restore fixture','test','error')", (self.project, self.user))
        self.phases = []

    async def asyncTearDown(self):
        await runtime.stop_runtime(self.project)
        for item in reversed(self.patches):
            item.stop()
        with jobs.connection() as conn:
            conn.execute('DELETE FROM projects WHERE id=%s', (self.project,))
            conn.execute('DELETE FROM users WHERE id=%s', (self.user,))
        self.tmp.cleanup()

    def checkpoint(self, html='<h1>Version one</h1>', build='mkdir -p dist; cp index.html dist/index.html', startup_command=None):
        command = 'python -m http.server $PORT --bind 127.0.0.1'
        (self.root / 'index.html').write_text(html)
        (self.root / '.atoms-workspace.json').write_text(json.dumps({'dev': command, 'build': build}))
        session = AgentSession(self.root)
        session.state = {'messages': [], 'completed': True, 'demo': {'ready': True, 'kind': 'web'}}
        session.save('old demo')
        files = agent.snapshot_files(self.root)
        state = capture_runtime_state(self.root, startup_command or command)
        state['data']={'format':1,'id':str(uuid.uuid4())}
        with jobs.connection() as conn:
            conn.execute('INSERT INTO project_versions(project_id,version,summary,files,preview_html,runtime_state) VALUES(%s,1,\'fixture\',%s,\'old html\',%s)', (self.project, Jsonb(files), Jsonb(state)))
            conn.execute("INSERT INTO project_restores(project_id,id,version,status,result) VALUES(%s,%s,1,'running',%s)", (self.project, uuid.uuid4(), Jsonb({'previous_status': 'error'})))
            conn.execute("UPDATE projects SET status='restoring' WHERE id=%s", (self.project,))
        return files

    async def test_custom_restored_command_remains_active_after_browser_verification(self):
        command = 'python -m http.server $PORT --bind 127.0.0.1 --directory dist'
        self.checkpoint(startup_command=command)
        self.break_current_workspace()
        await restore_version(self.project, 1, progress=self.phases.append)
        status = runtime.runtime_status(self.project)
        self.assertEqual(status['command'], command)
        self.assertTrue(status['running'])
        with jobs.connection() as conn:
            result = conn.execute('SELECT result FROM project_restores WHERE project_id=%s', (self.project,)).fetchone()['result']
        self.assertEqual(result['restored_preview']['url'], status['url'])
        token = status['url'].split('/')[-2]
        self.assertIsNotNone(runtime.runtime_by_token(token))

    async def test_legacy_version_with_python_cache_rebuilds_without_restoring_cache(self):
        self.checkpoint()
        with jobs.connection() as conn:
            conn.execute("UPDATE project_versions SET files=files || %s, runtime_state=jsonb_set(runtime_state,'{modes}',runtime_state->'modes' || %s) WHERE project_id=%s AND version=1",
                         (Jsonb({'.python-cache/dependency-state.json':'obsolete cache'}), Jsonb({'.python-cache/dependency-state.json':420}), self.project))
        self.break_current_workspace()
        await restore_version(self.project, 1)
        self.assertTrue(runtime.runtime_status(self.project)['running'])
        self.assertFalse((self.root/'.python-cache/dependency-state.json').exists())
        with jobs.connection() as conn:
            files = conn.execute('SELECT files FROM project_versions WHERE project_id=%s AND version=2', (self.project,)).fetchone()['files']
        self.assertNotIn('.python-cache/dependency-state.json', files)

    def break_current_workspace(self):
        (self.root / 'index.html').write_text('<h1>Broken current version</h1>')
        (self.root / 'extra.txt').write_text('must disappear')
        (self.root / 'dist').mkdir()
        (self.root / 'dist/stale.js').write_text('stale build output')
        (self.root / 'node_modules').mkdir()
        (self.root / 'node_modules/stale').write_text('stale dependencies')
        (self.root / '.atoms-workspace.json').write_text(json.dumps({'dev': 'exit 5', 'build': 'exit 4'}))
        session = AgentSession(self.root)
        session.state = {'demo': {'ready': False, 'kind': 'artifact'}}
        session.save('failed latest task')

    async def test_old_new_old_switch_preserves_all_versions_and_complete_files(self):
        from agent_harness import source_digest
        executable = self.root / 'run.sh'
        executable.write_text('echo old')
        executable.chmod(0o755)
        old = self.checkpoint()
        (self.root/'index.html').write_text('<h1>New version</h1>')
        (self.root/'new-only.txt').write_text('new feature')
        executable.write_text('echo new')
        executable.chmod(0o700)
        new = agent.snapshot_files(self.root)
        state = capture_runtime_state(self.root)
        state['data']={'format':1,'id':str(uuid.uuid4())}
        with jobs.connection() as conn:
            conn.execute("INSERT INTO project_versions(project_id,version,summary,files,preview_html,runtime_state) VALUES(%s,2,'new',%s,'new',%s)", (self.project,Jsonb(new),Jsonb(state)))
            original = conn.execute('SELECT version,files,runtime_state FROM project_versions WHERE project_id=%s ORDER BY version', (self.project,)).fetchall()
        for target, expected, mode in [(1,old,0o755),(2,new,0o700),(1,old,0o755)]:
            with jobs.connection() as conn:
                conn.execute("UPDATE project_restores SET id=%s,status='running',version=%s,result=%s WHERE project_id=%s",(uuid.uuid4(),target,Jsonb({'previous_status':'ready'}),self.project))
                conn.execute("UPDATE projects SET status='restoring' WHERE id=%s",(self.project,))
            await restore_version(self.project, target)
            self.assertEqual(source_digest(agent.snapshot_files(self.root)), source_digest(expected))
            self.assertEqual(executable.stat().st_mode & 0o777, mode)
            self.assertEqual((self.root/'new-only.txt').exists(), target == 2)
            self.assertTrue(runtime.runtime_status(self.project)['running'])
            with jobs.connection() as conn:
                history = conn.execute('SELECT version,files,runtime_state FROM project_versions WHERE project_id=%s ORDER BY version', (self.project,)).fetchall()
            self.assertEqual(history[:2], original)
        self.assertEqual([row['version'] for row in history], [1,2,3,4,5])

    async def test_error_to_old_version_rebuilds_checks_browser_and_registers_new_route(self):
        files = self.checkpoint()
        self.break_current_workspace()
        version = await restore_version(self.project, 1, self.phases.append)
        self.assertEqual(version, 2)
        self.assertEqual((self.root / 'index.html').read_text(), files['index.html'])
        self.assertFalse((self.root / 'extra.txt').exists())
        self.assertFalse((self.root / 'dist/stale.js').exists())
        self.assertFalse((self.root / 'node_modules/stale').exists())
        self.assertEqual((self.root / 'dist/index.html').read_text(), files['index.html'])
        self.assertEqual(session_status(self.root)['demo']['kind'], 'web')
        status = runtime.runtime_status(self.project)
        self.assertTrue(status['running'])
        with jobs.connection() as conn:
            operation = conn.execute('SELECT * FROM project_restores WHERE project_id=%s', (self.project,)).fetchone()
            self.assertEqual(operation['status'], 'succeeded')
            self.assertEqual(operation['result']['restored_preview']['url'], status['url'])
            self.assertEqual(conn.execute('SELECT status FROM projects WHERE id=%s', (self.project,)).fetchone()['status'], 'ready')
            route = conn.execute('SELECT token_hash FROM runtime_routes WHERE project_id=%s', (self.project,)).fetchone()
            self.assertEqual(route['token_hash'], hashlib.sha256(status['url'].split('/')[-2].encode()).hexdigest())
        self.assertIn('正在恢复数据库和浏览器数据',self.phases)
        self.assertIn('正在校验数据库结构、数据和浏览器存储',self.phases)

    async def test_failed_build_restores_source_dependencies_artifacts_and_session(self):
        self.checkpoint(build='exit 7')
        self.break_current_workspace()
        before = agent.snapshot_files(self.root)
        with self.assertRaisesRegex(ValueError, '构建失败'):
            await restore_version(self.project, 1, self.phases.append)
        self.assertEqual(agent.snapshot_files(self.root), before)
        self.assertTrue((self.root / 'dist/stale.js').exists())
        self.assertTrue((self.root / 'node_modules/stale').exists())
        self.assertEqual(session_status(self.root)['demo']['kind'], 'artifact')
        with jobs.connection() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) AS n FROM project_versions WHERE project_id=%s', (self.project,)).fetchone()['n'], 1)
            self.assertEqual(conn.execute('SELECT status FROM project_restores WHERE project_id=%s', (self.project,)).fetchone()['status'], 'running')

    async def test_http_200_with_javascript_error_is_not_restore_success(self):
        self.checkpoint(html='<html><body><h1>Old page</h1><script>throw new Error("broken historic JavaScript")</script></body></html>')
        self.break_current_workspace()
        with self.assertRaisesRegex(ValueError, '预览验证失败.*broken historic JavaScript'):
            await restore_version(self.project, 1)
        self.assertIn('Broken current version', (self.root / 'index.html').read_text())
        self.assertTrue((self.root / 'dist/stale.js').exists())
        self.assertIsNone(runtime.runtime_status(self.project))

    async def test_restore_api_returns_accepted_and_survives_request_end(self):
        import importlib
        import httpx
        import main
        from unittest.mock import AsyncMock
        self.checkpoint()
        # The API starts from an errored build, not an already-running restore.
        with jobs.connection() as conn:
            conn.execute("UPDATE projects SET status='error' WHERE id=%s", (self.project,))
        with patch.dict(os.environ, {'PROJECT_ID': str(self.project), 'USER_ID': str(self.user), 'WORKER_TOKEN': 'test-worker'}):
            worker = importlib.import_module('project_worker')
        entered, release = asyncio.Event(), asyncio.Event()
        build = agent.run_build
        async def gated_build(project_id):
            entered.set()
            await release.wait()
            return await build(project_id)
        async def invoke(project_id, operation, payload):
            # Dispatch can wait for a container to start. It must not block
            # the restore task's exclusive lock for that entire request.
            with jobs.connection() as guard:
                acquired = guard.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,19)) AS acquired', (str(project_id),)).fetchone()['acquired']
                self.assertTrue(acquired)
                guard.execute('SELECT pg_advisory_unlock(hashtextextended(%s,19))', (str(project_id),))
            return await worker.invoke(project_id, operation, worker.InvokeRequest(**payload))
        main.app.dependency_overrides[main.current_user] = lambda: {'id': self.user}
        try:
            with patch.object(worker, 'PROJECT_ID', self.project), patch.object(worker, 'USER_ID', self.user), \
                 patch.object(main, 'agent_invoke', AsyncMock(side_effect=invoke)), \
                 patch.object(worker, 'ensure_workspace', return_value=self.root), patch('agent.run_build', gated_build):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
                    url = f'/api/projects/{self.project}'
                    legacy = await client.post(url + '/restore/1')
                    self.assertEqual(legacy.status_code, 409)
                    self.assertIn('本次未提交还原任务', legacy.json()['detail'])
                    self.assertFalse(entered.is_set())
                    response = await client.post(url + '/restore/1', headers={'X-Atoms-Restore-Protocol':'2'})
                    self.assertEqual(response.status_code, 202, response.text)
                    await asyncio.wait_for(entered.wait(), 10)
                    operation = (await client.get(url + '/restore')).json()['restore']
                    self.assertEqual(operation['id'], response.json()['restore_id'])
                    self.assertEqual(operation['status'], 'running')
                    self.assertIn('恢复依赖', operation['phase'])
                    self.assertEqual((await client.post(url + '/restore/1', headers={'X-Atoms-Restore-Protocol':'2'})).status_code, 409)
                    self.assertEqual((await client.put(url + '/assets/conflict.txt', content='conflict')).status_code, 409)
                    release.set()
                    await asyncio.wait_for(asyncio.gather(*worker._restore_tasks), 30)
                    operation = (await client.get(url + '/restore')).json()['restore']
                    self.assertEqual(operation['status'], 'succeeded', operation)
                    self.assertEqual(len((await client.get(url + '/versions')).json()), 2)
        finally:
            release.set()
            main.app.dependency_overrides.clear()

    async def test_worker_restart_recovers_incomplete_restore(self):
        import importlib
        self.checkpoint()
        with patch.dict(os.environ, {'PROJECT_ID': str(self.project), 'USER_ID': str(self.user), 'WORKER_TOKEN': 'test-worker'}):
            worker = importlib.import_module('project_worker')
        backup = WorkspaceBackup(self.root, {'status': 'error', 'dev_command': ''})
        backup.detach()
        (self.root / 'index.html').write_text('partial interrupted restore')
        with patch.object(worker, 'PROJECT_ID', self.project), patch.object(worker, 'USER_ID', self.user), \
             patch.object(worker, 'ensure_workspace', return_value=self.root):
            async with worker.lifespan(worker.app):
                self.assertEqual((self.root / 'index.html').read_text(), '<h1>Version one</h1>')
                with jobs.connection() as conn:
                    operation = conn.execute('SELECT status,error FROM project_restores WHERE project_id=%s', (self.project,)).fetchone()
                    self.assertEqual(operation['status'], 'failed')
                    self.assertIn('中断', operation['error'])
                    self.assertEqual(conn.execute('SELECT status FROM projects WHERE id=%s', (self.project,)).fetchone()['status'], 'error')

    async def test_worker_database_role_can_restore_only_its_project(self):
        import main
        from psycopg import sql
        from project_database import remove
        self.checkpoint()
        self.break_current_workspace()
        role = main.project_db_role(self.project)
        with jobs.connection() as conn:
            main.enable_project_rls(conn)
            main.grant_project_role(conn, self.project)
        admin_connection = jobs.connection
        def scoped_connection():
            conn = admin_connection()
            conn.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(role)))
            return conn
        try:
            with patch('jobs.connection', scoped_connection):
                self.assertEqual(await restore_version(self.project, 1), 2)
                with jobs.connection() as conn:
                    self.assertEqual(conn.execute('SELECT COUNT(*) AS n FROM project_restores').fetchone()['n'], 1)
                    self.assertEqual(conn.execute('SELECT COUNT(*) AS n FROM runtime_routes').fetchone()['n'], 1)
        finally:
            with admin_connection() as conn:
                remove(conn, self.project)
                conn.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
                conn.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
