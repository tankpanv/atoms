import json
import os
import socket
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import asyncio
import uvicorn
from starlette.responses import HTMLResponse
from pydantic import ValidationError
from agent import run_agent, verify_delivery_runtime
from agent_harness import TaskLedger, source_digest
from build_tools import browser_enabled, normalize_tools
from test_agent_harness import example_plan
from test_agent_execution import calls


class SelectionTests(unittest.TestCase):
    def test_api_defaults_and_unknown_tools(self):
        from main import ProjectCreate, MessageCreate, BuildToolsSelection
        self.assertEqual(ProjectCreate(prompt='calendar').enabled_tools, [])
        self.assertIsNone(MessageCreate(content='continue').enabled_tools)
        self.assertEqual(BuildToolsSelection(enabled_tools=['browser_check']).enabled_tools, ['browser_check'])
        with self.assertRaises(ValidationError):
            ProjectCreate(prompt='calendar', enabled_tools=['unknown'])
        with self.assertRaises(ValueError):
            normalize_tools('browser_check')

    def test_job_snapshots_explicit_tools_and_project_default(self):
        import jobs
        class Conn:
            def __init__(self): self.writes = []
            def execute(self, query, args):
                if query.startswith('SELECT status,build_tier'):
                    return SimpleNamespace(fetchone=lambda: {'status':'ready', 'build_tier':'normal', 'enabled_tools':['browser_check']})
                self.writes.append((query, args))
                return SimpleNamespace(fetchone=lambda: None)
        for selection, expected in ((None, ['browser_check']), ([], [])):
            conn = Conn()
            with patch('jobs.snapshot_experts', return_value=[]):
                jobs.enqueue_project(conn, uuid.uuid4(), 'calendar', 'test', expert_ids=[], enabled_tools=selection)
            snapshot = next(args for sql, args in conn.writes if sql.startswith('UPDATE agent_jobs SET expert_ids'))
            self.assertEqual(snapshot[2].obj, expected)

    def test_disabled_browser_uses_command_proofs_and_preserves_task_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {'frontend/package.json':'{}', 'frontend/src/pages/Calendar.tsx':'export default () => null', 'README.md':'calendar'}
            for name, value in files.items():
                p = root/name; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(value)
            original = example_plan()
            ledger = TaskLedger(root, original, 'calendar')
            ledger.tasks[0]['status'] = 'in_progress'; ledger.persist()
            disabled = {**original, 'enabled_tools':[]}
            ledger = TaskLedger(root, disabled, 'calendar')
            self.assertTrue(ledger.resumed)
            self.assertEqual(ledger.tasks[0]['status'], 'in_progress')
            proof = ledger.record('run_shell', 'business test', 0, 'pass', source_digest(files), ['R1'])
            ledger.update('T1', 'done', 'verified', [proof])
            self.assertEqual(ledger.completion_issues(files), [])
            ledger.plan['enabled_tools'] = ['browser_check']
            self.assertTrue(any('browser' in issue for issue in ledger.completion_issues(files)))


class RuntimeSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)/uuid.uuid4().hex; self.root.mkdir()
        self.root.joinpath('package.json').write_text(json.dumps({'scripts':{'build':'echo build'}}))
        self.root.joinpath('README.md').write_text('Calendar, build and launch')
        p = self.root/'frontend/src/pages/Calendar.tsx'; p.parent.mkdir(parents=True); p.write_text('export default () => null')
        (self.root/'greet.py').write_text('print("verified business")')
        self.plan = {**example_plan(), 'enabled_tools':[]}
        self.request_count = 0
        async def app(scope, receive, send):
            self.request_count += 1
            await HTMLResponse('<html><body>Actual calendar runtime</body></html>')(scope, receive, send)
        self.sock = socket.socket(); self.sock.bind(('127.0.0.1',0)); self.sock.listen()
        self.server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='off'))
        self.task = asyncio.create_task(self.server.serve(sockets=[self.sock]))
        for _ in range(100):
            if self.server.started: break
            await asyncio.sleep(.02)
        self.runtime = SimpleNamespace(services=[], port=self.sock.getsockname()[1], prefix='/', base_aware=True)

    async def asyncTearDown(self):
        self.server.should_exit=True; await self.task
        self.sock.close(); self.temp.cleanup()

    async def test_final_and_fallback_checks_require_real_startup_without_interaction_scene_generation(self):
        with patch('runtime.start_runtime', AsyncMock(return_value=self.runtime)), patch('agent_checks.start_runtime', AsyncMock(return_value=self.runtime)):
            for fallback in (False, True):
                code, report = await verify_delivery_runtime(uuid.uuid4(), self.root, self.plan, require_scenario=True,
                    preview_only=fallback, scene_resolver=AsyncMock(side_effect=AssertionError('disabled scene generated')))
                self.assertEqual(code, 0, report)
                self.assertIn('HTTP', report)
            self.assertGreaterEqual(self.request_count, 4)

    async def test_checked_option_keeps_real_browser_path(self):
        enabled = {**self.plan, 'enabled_tools':['browser_check']}
        browser = AsyncMock(return_value=(0,json.dumps({'text':'Actual calendar runtime'})))
        with patch('runtime.start_runtime', AsyncMock(return_value=self.runtime)), patch('agent_checks.browser_check', browser):
            code, report = await verify_delivery_runtime(uuid.uuid4(), self.root, enabled)
        self.assertEqual(code,0,report)
        browser.assert_awaited_once()

    async def test_agent_does_not_advertise_or_execute_disabled_browser_and_still_completes(self):
        sequence = [calls(('browser_check', {'actions':[]})),
                    calls(('run_shell', {'command':'python greet.py', 'requirement_ids':['R1']})),
                    calls(('update_task', {'id':'T1','status':'done','evidence_ids':['V1']})),
                    {'content':'Calendar verified with business tests and HTTP.'}]
        advertised = []
        class Gateway:
            def __init__(self,*args): pass
            async def chat(self, client, state, messages, tools=None, **kwargs):
                advertised.extend(t['function']['name'] for t in tools or [])
                return sequence.pop(0)
        with patch('agent.ensure_workspace', return_value=self.root), patch('agent.project_uid', return_value=os.getuid()), patch('agent.ModelGateway', Gateway), patch('agent.run_build', AsyncMock(return_value=(0,'actual build fixture'))), patch('model_catalog.catalog', return_value=[{'id':'test','context':128000}]), patch('runtime.start_runtime', AsyncMock(return_value=self.runtime)), patch('agent_checks.start_runtime', AsyncMock(return_value=self.runtime)):
            result = await run_agent(uuid.UUID(hex=self.root.name), 'Calendar', 'test', lambda *args,**kwargs:None,
                                     plan=json.dumps(self.plan), enabled_tools=[])
        self.assertNotIn('browser_check', advertised)
        self.assertIn('verified',result['summary'])
        self.assertEqual(result['delivery'], 'demo')
        self.assertFalse(json.loads((self.root/'.atoms/task-state.json').read_text())['completed'])
        from agent_session import AgentSession
        state = AgentSession(self.root).state
        self.assertTrue(state['demo']['ready'])
        self.assertFalse(state['demo']['core_interactions_verified'])
        self.assertFalse(state['demo']['browser_check_enabled'])


if __name__ == '__main__': unittest.main()
