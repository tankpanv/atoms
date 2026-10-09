"""Regressions grounded in the October 9 calendar authentication incident."""
import copy
import json
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import agent
from agent_delivery import DeliveryBudget, DeliveryLimitReached, budgeted_chat
from agent_session import AgentSession, repair_tool_boundaries
from coding_runtime import AgentState, ModelGateway
from execution_guard import ExecutionGuard
from system_contract import diagnosis_fingerprint
from test_agent_execution import calls
from test_project_templates import default_plan


class IncidentTests(unittest.IsolatedAsyncioTestCase):
    async def test_budget_wrapper_preserves_positional_output_and_reasoning_and_reserve(self):
        gateway = ModelGateway('test')
        original = AsyncMock(return_value={'content': 'verified'})
        gateway.chat = original
        budget = DeliveryBudget(1000000, 120, time.monotonic() + 60)
        budget.phase = 'stabilization'
        budget.repair_token_limit = 2500
        guards = []
        gateway.chat = budgeted_chat(gateway, budget, lambda **kwargs: guards.append(kwargs))
        messages = [{'role': 'user', 'content': 'Current goal'}]
        reasoning = {'effort': 'low'}
        result = await gateway.chat(None, AgentState.IMPLEMENT, messages, [], 8000, reasoning)
        self.assertEqual(result['content'], 'verified')
        self.assertEqual(original.await_args.kwargs['reasoning'], reasoning)
        self.assertLess(original.await_args.kwargs['max_tokens'], 2500)
        self.assertGreater(original.await_args.kwargs['max_tokens'], 512)
        self.assertTrue(guards)

    async def test_shell_does_not_turn_failed_assertion_into_successful_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / uuid.uuid4().hex
            root.mkdir()
            with patch('agent.ensure_workspace', return_value=root), patch('agent.project_uid', return_value=os.getuid()):
                code, output = await agent.run_shell(uuid.UUID(hex=root.name),
                    "python -c 'assert False, \"ACTUAL_FAILURE\"'; echo MISLEADING_SUCCESS")
                self.assertNotEqual(code, 0)
                self.assertIn('ACTUAL_FAILURE', output)
                self.assertNotIn('MISLEADING_SUCCESS', output)
                code, output = await agent.run_shell(uuid.UUID(hex=root.name),
                    "if test -f absent; then echo yes; fi; false || echo EXPECTED_FALLBACK")
                self.assertEqual(code, 0)
                self.assertIn('EXPECTED_FALLBACK', output)

    def test_stall_survives_restart_but_new_requirement_proof_counts_as_progress(self):
        state = {}
        tasks = [{'id': 'T1', 'status': 'in_progress'}]
        guard = ExecutionGuard(state)
        guard.observe('source', tasks)
        for _ in range(17):
            ExecutionGuard(state).observe('source', tasks)
        with self.assertRaisesRegex(DeliveryLimitReached, '18'):
            ExecutionGuard(state).observe('source', tasks)
        proof = [{'kind': 'run_shell', 'exit_code': 0, 'source_digest': 'source', 'requirement_ids': ['R1']}]
        self.assertFalse(ExecutionGuard(state).observe('source', tasks, proof))

    def test_feedback_replaces_stale_advice_without_breaking_pairs_or_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            session = AgentSession(Path(directory))
            call = calls(('read_file', {'path': 'src/App.tsx'})) | {'role': 'assistant'}
            result = {'role': 'tool', 'tool_call_id': call['tool_calls'][0]['id'], 'content': 'original source'}
            messages = [{'role': 'user', 'content': 'Keep registration and login'}, call, result]
            for i in range(10):
                session.feedback(messages, 'recovery', f'current diagnosis {i}')
            self.assertEqual(len(messages), 4)
            self.assertEqual(messages, repair_tool_boundaries(messages))
            self.assertIn('current diagnosis 9', messages[-1]['content'])
            with session.connect() as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM events WHERE kind='coordinator_feedback'").fetchone()[0], 10)

    def test_diagnosis_cache_ignores_timing_receipt_rotation_but_not_new_probes(self):
        facts = {'source_digest': 'source', 'tasks': [{'id': 'T1', 'status': 'done'}],
                 'acceptance_gaps': [], 'successful_probes': ['auth CRUD verified'],
                 'recent_receipts': [{'id': 'V20', 'exit_code': 0, 'result': 'elapsed 12ms'}],
                 'actual_failed_operations': [{'name': 'run_shell', 'args': {'command': 'check', 'timeout': 10},
                     'source': 'source', 'code': 1, 'output': 'AssertionError: missing route at foo:12'}]}
        changed = copy.deepcopy(facts)
        changed['recent_receipts'] = []
        changed['actual_failed_operations'][0]['output'] = 'AssertionError: missing route at foo:42'
        changed['actual_failed_operations'][0]['args']['timeout'] = 90
        self.assertEqual(diagnosis_fingerprint(facts), diagnosis_fingerprint(changed))
        changed['successful_probes'].append('actual request encoding verified')
        self.assertNotEqual(diagnosis_fingerprint(facts), diagnosis_fingerprint(changed))

    def test_small_change_uses_bounded_working_context_and_honors_explicit_override(self):
        with patch.dict(os.environ, {'AGENT_CONTEXT_TOKENS': ''}):
            self.assertEqual(agent.preferred_context_tokens(1000000, 'medium'), 48000)
            self.assertEqual(agent.preferred_context_tokens(1000000, 'simple'), 32000)
        with patch.dict(os.environ, {'AGENT_CONTEXT_TOKENS': '64000'}):
            self.assertEqual(agent.preferred_context_tokens(1000000, 'simple'), 64000)

    def test_vite_config_base_path_is_preserved_even_without_command_flag(self):
        from runtime import base_path_aware, detected_command
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = 'npm run dev -- --host 0.0.0.0 --port $PORT --strictPort'
            (root / '.atoms-workspace.json').write_text(json.dumps({'dev': command}))
            self.assertFalse(base_path_aware(root, command))
            (root / 'vite.config.ts').write_text("const base = process.env.BASE_PATH || '/'; export default {base}")
            with patch('runtime.ensure_workspace', return_value=root):
                self.assertEqual(detected_command(uuid.uuid4()), (command, True))
            self.assertFalse(base_path_aware(root, 'python server.py'))
            (root / '.atoms-workspace.json').write_text(json.dumps({'dev': command, 'base_aware': False}))
            self.assertFalse(base_path_aware(root, command))

    async def test_http_tool_accepts_runtime_path_without_duplicate_prefix(self):
        from types import SimpleNamespace
        import httpx
        from agent_checks import http_request
        prefix = '/api/runtime/current-project-token'
        real_client = httpx.AsyncClient
        seen = []
        def handler(request):
            seen.append(request.url.path)
            return httpx.Response(200, json={'ok': True}) if request.url.path == prefix + '/api/auth/me' else httpx.Response(404)
        def client(**kwargs):
            return real_client(transport=httpx.MockTransport(handler), **kwargs)
        runtime = SimpleNamespace(prefix=prefix, base_aware=True, port=12345, services=[])
        with patch('agent_checks.start_runtime', AsyncMock(return_value=runtime)), patch('agent_checks.httpx.AsyncClient', side_effect=client):
            for path in ('/api/auth/me', prefix + '/api/auth/me'):
                code, output = await http_request(uuid.uuid4(), {'path': path, 'expect_status': 200, 'expect_json': {'ok': True}})
                self.assertEqual(code, 0, output)
        self.assertEqual(seen, [prefix + '/api/auth/me'] * 2)

    async def test_explicit_auth_api_acceptance_can_run_register_and_login_in_normal_tier(self):
        sequence = [calls(('http_request', {'path': '/api/auth/register', 'method': 'POST', 'expect_status': 201, 'requirement_ids': ['R1']})),
                    calls(('http_request', {'path': '/api/auth/login', 'method': 'POST', 'expect_status': 200, 'requirement_ids': ['R1']}))]
        class Gateway:
            def __init__(self, *args): pass
            async def chat(self, client, state, messages, *args, **kwargs):
                if not sequence:
                    raise AssertionError(json.dumps(messages[-4:], ensure_ascii=False))
                return sequence.pop(0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / uuid.uuid4().hex
            (root / 'src').mkdir(parents=True)
            (root / 'src/App.tsx').write_text('export default function App() { return <div>Calendar</div> }')
            (root / 'README.md').write_text('Existing calendar')
            plan = default_plan(True)
            plan['architecture']['frontend']['directory'] = 'src'
            plan['requirements'][0].update(verification='api', origin='explicit')
            request = AsyncMock(return_value=(0, json.dumps({'status': 200, 'passed': True, 'body': '{}'})))
            with patch('agent.ensure_workspace', return_value=root), patch('agent.project_uid', return_value=os.getuid()), \
                 patch('agent.ModelGateway', Gateway), patch('agent_checks.http_request', request), \
                 patch('model_catalog.catalog', return_value=[{'id': 'test', 'context': 1000000}]):
                with self.assertRaises(agent.AgentStopped):
                    await agent.run_agent(uuid.UUID(hex=root.name), '增加注册登录功能', 'test', lambda *a, **kw: None,
                        plan=json.dumps(plan), build_tier='normal', should_stop=lambda: request.await_count >= 2)
            self.assertEqual(request.await_count, 2)
            state = json.loads((root / '.atoms/task-state.json').read_text())
            self.assertEqual(len([e for e in state['evidence'] if e['kind'] == 'http_request']), 2)
