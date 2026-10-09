import copy
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

from build_tiers import (normalize_tier, tier_instructions, stamp_plan,
                         continuation_for_tier, tier_budget_limits, worker_budget_changed)
from agent_delivery import DeliveryBudget
import time
from agent import make_plan, run_agent, TOOLS
from agent_harness import TaskLedger
from agent_session import AgentSession
from coding_runtime import AgentState
from delivery_checks import resolve_scene
from jobs import enqueue_project
from main import ProjectCreate, MessageCreate
from pydantic import ValidationError
from test_agent_execution import cli_plan, calls


class TierPolicyTests(unittest.TestCase):
    def test_configured_limits_scale_once_without_hidden_iteration_cap(self):
        settings = {'AGENT_MAX_TOKENS': '100001', 'AGENT_MAX_ITERATIONS': '301'}
        for tier, multiplier in [('normal', 1), ('deep', 2), ('advanced', 3)]:
            limits = tier_budget_limits(tier, settings)
            self.assertEqual(limits['token_limit'], 100001 * multiplier)
            self.assertEqual(limits['iteration_limit'], 301 * multiplier)
            self.assertEqual(limits['base_iteration_limit'], 301)
            budget = DeliveryBudget(limits['token_limit'], limits['iteration_limit'], time.monotonic() + 3600)
            self.assertEqual(budget.repair_token_limit, 100001 * multiplier // 2)
            self.assertEqual(budget.repair_iteration_limit, 301 * multiplier // 2)

    def test_custom_multiplier_and_invalid_configuration(self):
        limits = tier_budget_limits('deep', {'AGENT_MAX_TOKENS': '200000', 'AGENT_MAX_ITERATIONS': '100',
                                            'AGENT_BUILD_DEEP_MULTIPLIER': '4'})
        self.assertEqual(limits['token_limit'], 800000)
        self.assertEqual(limits['iteration_limit'], 400)
        for invalid in ('0', '-1', '1.5', 'bad'):
            with self.assertRaisesRegex(ValueError, '正整数'):
                tier_budget_limits('deep', {'AGENT_BUILD_DEEP_MULTIPLIER': invalid})

    def test_worker_adopts_changed_or_removed_budget_settings(self):
        settings = {'AGENT_MAX_TOKENS': '15000000', 'AGENT_MAX_ITERATIONS': '200',
                    'AGENT_BUILD_DEEP_MULTIPLIER': '2'}
        worker = [f'{key}={value}' for key, value in settings.items()]
        self.assertFalse(worker_budget_changed(worker, settings))
        self.assertTrue(worker_budget_changed(worker, settings | {'AGENT_BUILD_DEEP_MULTIPLIER': '4'}))
        self.assertTrue(worker_budget_changed(worker, {'AGENT_MAX_TOKENS': '15000000'}))
        self.assertFalse(worker_budget_changed([], {}))
        self.assertFalse(worker_budget_changed(['AGENT_BUILD_DEEP_MULTIPLIER='], {}))

    def test_fallback_uses_effective_tier_limits(self):
        for tier, multiplier in [('normal', 1), ('deep', 2), ('advanced', 3)]:
            limits = tier_budget_limits(tier, {})
            budget = DeliveryBudget(limits['token_limit'], limits['iteration_limit'], time.monotonic() + 3600)
            budget.task_calls = 160 * multiplier - 1
            self.assertFalse(budget.due())
            budget.task_calls += 1
            self.assertTrue(budget.due())
            budget.task_calls = 0
            budget.task_tokens = 12750000 * multiplier - 1
            self.assertFalse(budget.due())
            budget.task_tokens += 1
            self.assertTrue(budget.due())

    def test_api_defaults_inheritance_and_invalid_values(self):
        self.assertEqual(ProjectCreate(prompt='Build').build_tier, 'normal')
        self.assertIsNone(MessageCreate(content='Continue').build_tier)
        for tier in ('normal', 'deep', 'advanced'):
            self.assertEqual(ProjectCreate(prompt='Build', build_tier=tier).build_tier, tier)
        for value in ('ultra', '', 1):
            with self.assertRaises(ValidationError):
                MessageCreate(content='Continue', build_tier=value)
        with self.assertRaises(ValueError):
            normalize_tier('ultra')

    def test_job_snapshot_inherits_project_and_can_override(self):
        for chosen, expected in ((None, 'deep'), ('advanced', 'advanced')):
            conn = MagicMock()
            conn.execute.return_value.fetchone.return_value = {'status': 'ready', 'build_tier': 'deep'}
            with patch('jobs.snapshot_experts', return_value=[]):
                enqueue_project(conn, uuid.uuid4(), 'Build', 'test', expert_ids=[], build_tier=chosen)
            insert = next(call for call in conn.execute.call_args_list if 'INSERT INTO agent_jobs' in call.args[0])
            self.assertEqual(insert.args[1][-1], expected)

    def test_scope_enforcement_and_required_rationale(self):
        for tier in ('normal', 'deep', 'advanced'):
            self.assertIn('用户明确要求', tier_instructions(tier))
            self.assertIn('不因档位自动引入支付', tier_instructions(tier))
        plan = cli_plan()
        plan['requirements'][0].update(origin='extension', rationale='Direct user value')
        for tier in ('normal', 'deep'):
            with self.assertRaisesRegex(ValueError, '仅高级档'):
                stamp_plan(copy.deepcopy(plan), tier)
        self.assertEqual(stamp_plan(plan, 'advanced')['build_tier'], 'advanced')
        del plan['requirements'][0]['rationale']
        with self.assertRaisesRegex(ValueError, 'rationale'):
            stamp_plan(plan, 'advanced')

    def test_normal_tier_is_explicitly_demo_first(self):
        instructions = tier_instructions('normal', 'verification')
        self.assertIn('前端首屏', instructions)
        self.assertIn('API 依赖失败记为 TODO', instructions)
        self.assertIn('不要求逐接口', instructions)

    def test_continue_with_changed_tier_preserves_original_request(self):
        plan = stamp_plan(cli_plan(), 'deep')
        continuation = ('Create greeting', plan)
        self.assertEqual(continuation_for_tier(continuation, 'deep'), continuation)
        self.assertEqual(continuation_for_tier(continuation, 'advanced'), ('Create greeting', None))
        self.assertEqual(continuation_for_tier(None, 'normal'), (None, None))
        self.assertEqual(continuation_for_tier(continuation, 'deep', reuse_allowed=False), ('Create greeting', None))
        old_policy = copy.deepcopy(plan)
        old_policy['build_policy_version'] = 'outdated'
        self.assertEqual(continuation_for_tier(('Create greeting', old_policy), 'deep'), ('Create greeting', None))
        # Durable checkpoints from before tiers remain normal.
        self.assertIsNotNone(continuation_for_tier(('Old goal', cli_plan()), 'normal')[1])


class TierPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        catalog = patch('model_catalog.catalog', return_value=[{'id': 'test', 'context': 128000}])
        catalog.start()
        self.addCleanup(catalog.stop)

    async def test_planning_cache_separates_tiers_and_reuses_same_tier(self):
        requests = []
        class Gateway:
            def __init__(self, *args): pass
            async def chat(self, client, state, messages, *args, **kwargs):
                requests.append(messages[0]['content'])
                return {'content': json.dumps(cli_plan())}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.atoms').mkdir()
            with patch('agent.ensure_workspace', return_value=root), patch('agent.ModelGateway', Gateway), patch('agent.project_uid', return_value=os.getuid()):
                for tier in ('normal', 'deep', 'advanced'):
                    plan = json.loads(await make_plan(uuid.uuid4(), 'Create greeting', 'test', build_tier=tier))
                    self.assertEqual(plan['build_tier'], tier)
                    self.assertIn(f'本次档位 {tier}', requests[-1])
                before = len(requests)
                await make_plan(uuid.uuid4(), 'Create greeting', 'test', build_tier='advanced')
                self.assertEqual(len(requests), before)
            self.assertEqual(len(requests), 3)

    async def test_execution_uses_tier_and_real_tools_with_scaled_budget(self):
        for tier in ('normal', 'deep', 'advanced'):
            sequence = [calls(('run_shell', {'command': 'python greet.py', 'requirement_ids': ['R1']})),
                        calls(('update_task', {'id': 'T1', 'status': 'done', 'evidence_ids': ['V1'], 'note': 'Verified output'})),
                        {'content': 'Greeting implemented and verified'}]
            requests = []
            class Gateway:
                def __init__(self, *args): pass
                async def chat(self, client, state, messages, *args, **kwargs):
                    requests.append(messages[0]['content'])
                    if state == AgentState.REVIEW:
                        return {'content': '{"approved":true,"issues":[],"summary":"Verified"}'}
                    return sequence.pop(0)
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'greet.py').write_text('print("hello")\n')
                (root / 'README.md').write_text('Run python greet.py')
                planned = cli_plan() if tier == 'normal' else stamp_plan(cli_plan(), tier)
                if tier == 'normal':
                    TaskLedger(root, planned, 'Create greeting')
                limits_env = {'AGENT_MAX_ITERATIONS': '200', 'AGENT_MAX_TOKENS': '15000000',
                              'AGENT_BUILD_NORMAL_MULTIPLIER': '1', 'AGENT_BUILD_DEEP_MULTIPLIER': '2',
                              'AGENT_BUILD_ADVANCED_MULTIPLIER': '3'}
                with patch('agent.ensure_workspace', return_value=root), patch('agent.ModelGateway', Gateway), patch('agent.project_uid', return_value=os.getuid()), patch.dict(os.environ, limits_env):
                    result = await run_agent(uuid.uuid4(), 'Create greeting', 'test', lambda *a, **kw: None,
                                             plan=json.dumps(planned), build_tier=tier)
                self.assertLessEqual(len(sequence), 1)  # No extra model summary after real acceptance.
                self.assertIn(f'本次档位 {tier}', requests[0])
                self.assertNotEqual(result.get('delivery'), 'demo')
                budget = AgentSession(root).state['delivery_budget']
                multiplier = {'normal': 1, 'deep': 2, 'advanced': 3}[tier]
                self.assertEqual(budget['iteration_limit'], 200 * multiplier)
                self.assertEqual(budget['token_limit'], 15000000 * multiplier)
                self.assertEqual(budget['repair_iteration_limit'], 100 * multiplier)
                self.assertEqual(budget['repair_token_limit'], 7500000 * multiplier)
                self.assertEqual(budget['budget_multiplier'], multiplier)
                self.assertEqual(json.loads((root / '.atoms/task-state.json').read_text())['plan'], planned)

    async def test_acceptance_receives_same_tier_and_uses_concrete_actions(self):
        requests = []
        class Gateway:
            async def chat(self, client, state, messages, *args, **kwargs):
                requests.append(messages[0]['content'])
                return {'content': '{"path":"/","actions":[{"action":"click","selector":"#show"},{"action":"assert_text","selector":"#result","value":"hello"}]}'}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = AgentSession(root)
            for tier in ('normal', 'deep', 'advanced'):
                plan = stamp_plan(cli_plan(), tier)
                scene = await resolve_scene(root, plan, {'text': 'hello', 'controls': ['#show', '#result']}, session, Gateway(), None, TOOLS)
                self.assertIn(f'本次档位 {tier}', requests[-1])
                self.assertEqual(scene['actions'][-1]['action'], 'assert_text')


@unittest.skipUnless(os.getenv('RUN_TIER_DATABASE_TESTS') == '1', 'requires live PostgreSQL and coordinator')
class TierDatabaseTests(unittest.TestCase):
    def test_real_api_persistence_inheritance_and_immutable_job_snapshot(self):
        from main import app, connection, current_user
        from fastapi.testclient import TestClient
        from account import ensure_profile
        user = {'id': uuid.uuid4(), 'email': 'tier-' + uuid.uuid4().hex + '@example.test'}
        projects = []
        client = TestClient(app)
        app.dependency_overrides[current_user] = lambda: user
        with connection() as conn:
            conn.execute("INSERT INTO users(id,email,password_hash) VALUES(%s,%s,'test-only')", (user['id'], user['email']))
            ensure_profile(conn, user)
        try:
            # The dispatcher is isolated so these API persistence checks never
            # bill a model or race a worker; SQL and project cleanup are real.
            with patch('main.dispatch_project', new_callable=AsyncMock), patch('main.catalog_ids', return_value=['test']):
                for tier in ('normal', 'deep', 'advanced'):
                    response = client.post('/api/projects', json={'prompt': 'Tier persistence check', 'model': 'test', 'build_tier': tier})
                    self.assertEqual(response.status_code, 201, response.text)
                    project = response.json()
                    projects.append(project['id'])
                    self.assertEqual(project['build_tier'], tier)
                identifier = projects[-1]
                response = client.post('/api/projects/' + identifier + '/messages', json={'content': 'Add detail', 'model': 'test'})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['messages'][-1]['build_tier'], 'advanced')
                response = client.post('/api/projects/' + identifier + '/messages', json={'content': 'New normal request', 'model': 'test', 'build_tier': 'normal'})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['build_tier'], 'normal')
                with connection() as conn:
                    jobs = conn.execute('SELECT build_tier FROM agent_jobs WHERE project_id=%s ORDER BY created_at', (identifier,)).fetchall()
                self.assertEqual([job['build_tier'] for job in jobs], ['advanced', 'advanced', 'normal'])
                invalid = client.post('/api/projects/' + identifier + '/messages', json={'content': 'Bad tier', 'build_tier': 'ultra'})
                self.assertEqual(invalid.status_code, 422)
        finally:
            for identifier in reversed(projects):
                self.assertEqual(client.delete('/api/projects/' + identifier).status_code, 204)
            app.dependency_overrides.clear()
            client.close()
            with connection() as conn:
                conn.execute('DELETE FROM users WHERE id=%s', (user['id'],))


if __name__ == '__main__':
    unittest.main()
