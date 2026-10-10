"""Planning failures recover through real read tools and model-directed patches."""
import copy
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from agent import make_plan, phase_chat
from agent_session import AgentSession, digest
from billing_context import active_job
from code_locator import CodeLocator, ChangeMapError, validate_change_map
from incremental_planning import assemble_plan
from planning_recovery import (PlanningRecovery, PlanningCheckpointReached,
                               merge_patch, unfinished_planning_request)
from project_templates import planner_contract
from coding_runtime import AgentState
from test_agent_execution import cli_plan, calls


class PlanningRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.root.joinpath('.atoms').mkdir()
        self.root.joinpath('greet.py').write_text('print("hello")\n')
        self.plan = cli_plan()
        self.plan['change_map'] = [{'path': 'greet.py', 'operation': 'modify', 'reason': 'Requested behavior', 'requirement_ids': ['R1']}]
        self.root.joinpath('.atoms/task-state.json').write_text(json.dumps({'request': 'Old task', 'plan': self.plan, 'completed': True}))
        catalog = patch('model_catalog.catalog', return_value=[{'id': 'model', 'context': 128000}])
        catalog.start()
        self.addCleanup(catalog.stop)

    async def run_planner(self, replies, **options):
        observed = []
        class Gateway:
            def __init__(self, *args): pass
            async def chat(self, client, state, messages, tools=None, **kwargs):
                observed.append({'tools': copy.deepcopy(tools), 'messages': copy.deepcopy(messages)})
                if not replies:
                    raise AssertionError('Unexpected model request')
                return replies.pop(0)
        with patch('agent.ensure_workspace', return_value=self.root), patch('agent.project_uid', return_value=os.getuid()), patch('agent.ModelGateway', Gateway):
            result = await make_plan(uuid.uuid4(), 'Add shared persistence', 'model', **options)
        self.assertFalse(replies)
        return json.loads(result), observed

    def invalid_paths(self):
        plan = copy.deepcopy(self.plan)
        for name in ('backend/app/routes.py', 'backend/app/db.py'):
            plan['change_map'].append({'path': name, 'operation': 'modify', 'reason': 'Add persistence', 'requirement_ids': ['R1']})
            plan['tasks'][0]['files'].append(name)
        return plan

    def test_mapping_reports_all_conflicts_without_changing_operations(self):
        locator = CodeLocator(self.root, AgentSession(self.root)); locator.sync()
        candidate = self.invalid_paths()
        before = copy.deepcopy(candidate)
        with self.assertRaises(ChangeMapError) as caught:
            validate_change_map(candidate, locator)
        issues = caught.exception.issues
        self.assertEqual([i['path'] for i in issues], ['backend/app/routes.py', 'backend/app/db.py'])
        self.assertTrue(caught.exception.needs_evidence)
        self.assertTrue(all(i['exists'] is False and i['parent_exists'] is False for i in issues))
        self.assertEqual(candidate, before)

    def test_only_empty_optional_commands_are_normalized(self):
        candidate = copy.deepcopy(self.plan)
        candidate['commands'].pop('bootstrap'); candidate['commands'].pop('test')
        result = assemble_plan(candidate, self.plan)
        self.assertEqual(result['commands']['bootstrap'], [])
        self.assertEqual(result['commands']['test'], [])
        self.assertNotIn('bootstrap', candidate['commands'])
        candidate['commands'].pop('dev')
        with self.assertRaisesRegex(ValueError, '启动命令'):
            assemble_plan(candidate, self.plan)
        candidate['commands']['dev'] = ''
        candidate['commands']['bootstrap'] = 'invalid command'
        with self.assertRaisesRegex(ValueError, 'bootstrap'):
            assemble_plan(candidate, self.plan)

    def test_existing_project_prompt_reports_absent_backend_without_scaffold_promises(self):
        contract = planner_contract('Add persistence', existing_files=['frontend/src/App.tsx', 'frontend/package.json'])
        self.assertIn('"backend/app/db.py": false', contract)
        self.assertIn('不会为这次改动重新安装', contract)
        self.assertNotIn('后端启动、路径代理、PostgreSQL Schema 持久化、一键Docker部署已配置', contract)
        installed = planner_contract('Add persistence', existing_files=['backend/app/db.py', 'frontend/src/App.tsx'])
        self.assertIn('"backend/app/db.py": true', installed)
        fresh = planner_contract('Build an application', existing_files=['.atoms/README.md'])
        self.assertIn('执行器自动安装模板', fresh)

    async def test_evidence_tools_reopen_after_synthesis_and_model_chooses_new_modules(self):
        invalid = self.invalid_paths(); invalid['commands'].pop('bootstrap')
        corrected = copy.deepcopy(invalid['change_map'])
        for entry in corrected[1:]: entry['operation'] = 'create'
        replies = [calls(('read_file', {'path': 'greet.py'})), calls(('list_files', {})), calls(('list_files', {})),
                   {'content': json.dumps(invalid)}, calls(('glob_files', {'pattern': 'backend/**/*.py'})),
                   {'content': json.dumps({'plan_patch': {'change_map': corrected}})}]
        result, observed = await self.run_planner(replies)
        self.assertEqual(observed[3]['tools'], [])
        self.assertTrue(observed[4]['tools'])
        feedback = observed[4]['messages'][-1]['content']
        self.assertIn('backend/app/routes.py', feedback); self.assertIn('backend/app/db.py', feedback)
        self.assertIn('"parent_exists": false', feedback)
        self.assertEqual(result['commands']['bootstrap'], [])
        self.assertEqual(result['goal'], invalid['goal'])
        self.assertEqual([e['operation'] for e in result['change_map']], ['modify', 'create', 'create'])
        self.assertFalse(self.root.joinpath('backend').exists())
        self.assertIn('"paths":[]', observed[5]['messages'][-1]['content'])

    async def test_multiple_distinct_schema_errors_repair_without_full_regeneration(self):
        invalid = copy.deepcopy(self.plan)
        invalid['application_type'] = 'invalid'
        invalid['architecture']['backend'].pop('stack')
        invalid['commands'].pop('dev')
        replies = [{'content': json.dumps(invalid)},
                   {'content': json.dumps({'plan_patch': {'application_type': 'cli'}})},
                   {'content': json.dumps({'plan_patch': {'architecture': {'backend': {'stack': 'none'}}}})},
                   {'content': json.dumps({'plan_patch': {'commands': {'dev': ''}}})}]
        result, observed = await self.run_planner(replies)
        self.assertEqual(len(observed), 4)
        self.assertTrue(all(not item['tools'] for item in observed[1:]))
        self.assertEqual(result['requirements'], self.plan['requirements'])
        self.assertEqual(result['architecture'], self.plan['architecture'])

    async def test_no_progress_saves_draft_and_manual_continue_resumes_original_goal(self):
        invalid = self.invalid_paths()
        old = self.root.joinpath('.atoms/task-state.json').read_text()
        replies = [{'content': json.dumps(invalid)} for _ in range(3)]
        with self.assertRaises(PlanningCheckpointReached):
            await self.run_planner(replies)
        phase = AgentSession(self.root).saved_phase('planning')
        self.assertFalse(phase['completed']); self.assertEqual(phase['candidate'], invalid)
        self.assertEqual(self.root.joinpath('.atoms/task-state.json').read_text(), old)
        self.assertEqual(unfinished_planning_request(self.root, '继续'), 'Add shared persistence')
        self.assertIsNone(unfinished_planning_request(self.root, 'Add another feature'))
        corrected = copy.deepcopy(invalid['change_map'])
        for entry in corrected[1:]: entry['operation'] = 'create'
        token = active_job.set('next-manual-job')
        try:
            result, observed = await self.run_planner([{'content': json.dumps({'plan_patch': {'change_map': corrected}})}],
                                                      resume_planning=True, history=[{'role': 'user', 'content': '继续'}])
        finally:
            active_job.reset(token)
        self.assertEqual(len(observed), 1)
        self.assertEqual(result['goal'], invalid['goal'])
        self.assertIsNone(unfinished_planning_request(self.root, '继续'))

    async def test_read_recovery_reserves_a_synthesis_after_two_tool_rounds(self):
        invalid = self.invalid_paths()
        corrected = copy.deepcopy(invalid['change_map'])
        for entry in corrected[1:]: entry['operation'] = 'create'
        result, observed = await self.run_planner([
            {'content': json.dumps(invalid)}, calls(('glob_files', {'pattern': 'backend/**/*.py'})),
            calls(('glob_files', {'pattern': 'backend/**/*.py'})),
            {'content': json.dumps({'plan_patch': {'change_map': corrected}})}])
        self.assertTrue(observed[1]['tools']); self.assertTrue(observed[2]['tools'])
        self.assertEqual(observed[3]['tools'], [])
        self.assertEqual(result['goal'], invalid['goal'])

    def test_repeated_tool_results_do_not_reset_stagnation_or_budgets(self):
        session = AgentSession(self.root)
        phase = session.phase('planning', 'test', [])
        recovery = PlanningRecovery(session, phase, 3, True)
        locator = CodeLocator(self.root, session); locator.sync()
        error = ChangeMapError([{'code': 'missing_source', 'message': 'missing.py absent'}])
        recovery.observe('glob_files', {'pattern': '*.py'}, 'greet.py')
        recovery.feedback(error, locator)
        recovery.observe('glob_files', {'pattern': '*.py'}, 'greet.py')
        recovery.feedback(error, locator)
        with self.assertRaises(PlanningCheckpointReached): recovery.feedback(error, locator)
        self.assertEqual(len(recovery.state['facts']), 1)
        recovery.state['calls'] = 16
        with self.assertRaises(PlanningCheckpointReached): recovery.before_call([], [])
        resumed = PlanningRecovery(session, session.saved_phase('planning'), 3, True)
        self.assertEqual(resumed.state['calls'], 16)
        with self.assertRaises(PlanningCheckpointReached): resumed.before_call([], [])

    def test_cached_read_wording_is_not_new_source_evidence(self):
        session = AgentSession(self.root)
        recovery = PlanningRecovery(session, session.phase('planning', 'test', []), 3, True)
        versions = {'greet.py': 'same-source-version'}
        recovery.observe('read_file', {'path': 'greet.py'}, 'full source', versions)
        recovery.observe('read_file', {'path': 'greet.py'}, 'already read; unchanged', versions)
        self.assertEqual(len(recovery.state['facts']), 1)

    def test_new_user_message_resets_budget_but_same_job_resume_keeps_usage(self):
        session = AgentSession(self.root)
        phase = session.phase('planning', 'same-goal', [])
        token = active_job.set('first-message')
        try:
            first = PlanningRecovery(session, phase, 3, True)
            first.state.update(calls=16, tokens=15000000)
            resumed = PlanningRecovery(session, phase, 3, True)
            self.assertEqual(resumed.state['tokens'], 15000000)
            with self.assertRaises(PlanningCheckpointReached):
                resumed.before_call([], [])
        finally:
            active_job.reset(token)
        token = active_job.set('second-message')
        try:
            next_message = PlanningRecovery(session, phase, 3, True)
            self.assertEqual(next_message.state['tokens'], 0)
            self.assertEqual(next_message.state['calls'], 0)
            next_message.before_call([], [])
        finally:
            active_job.reset(token)

    def test_default_planning_budget_uses_this_job_tier_and_not_legacy_240k(self):
        session = AgentSession(self.root)
        with patch.dict('os.environ', {'AGENT_MAX_TOKENS': '15000000', 'AGENT_PLANNING_MAX_TOKENS': '',
                                       'AGENT_BUILD_DEEP_MULTIPLIER': '2'}):
            phase = session.phase('planning', 'test', [])
            recovery = PlanningRecovery(session, phase, 3, True)
            recovery.state['tokens'] = 213054
            recovery.before_call([{'role': 'user', 'content': '中' * 46350}], [])
            self.assertEqual(recovery.token_limit, 15000000)
            phase['build_tier'] = 'deep'
            self.assertEqual(PlanningRecovery(session, phase, 3, True).token_limit, 30000000)

    def test_explicit_sublimit_caps_completion_and_reports_current_message_usage(self):
        session = AgentSession(self.root)
        with patch.dict('os.environ', {'AGENT_PLANNING_MAX_TOKENS': '2500'}):
            recovery = PlanningRecovery(session, session.phase('planning', 'test', []), 3, True)
            recovery.state['tokens'] = 1000
            ceiling = recovery.before_call([], [], 128000)
            self.assertEqual(ceiling, 2500 - 1000 - recovery.input_tokens)
            recovery.state['tokens'] = 2200
            with self.assertRaisesRegex(PlanningCheckpointReached, '本次对话.*已用 2,200.*上限 2,500'):
                recovery.before_call([], [])

    def test_real_usage_counts_instead_of_conservative_context_estimate(self):
        session = AgentSession(self.root)
        recovery = PlanningRecovery(session, session.phase('planning', 'test', []), 3, True)
        recovery.before_call([{'role': 'user', 'content': '中' * 10000}], [])
        recovery.record_response({'content': 'OK', '_response_meta': {
            'prompt_tokens': 1000, 'completion_tokens': 10, 'total_tokens': 1010}})
        self.assertEqual(recovery.state['tokens'], 1010)
        # Older compatible gateways can omit total_tokens while returning
        # actual prompt/completion counts, including a zero completion.
        recovery.record_response({'content': '', '_response_meta': {'prompt_tokens': 500, 'completion_tokens': 0}})
        self.assertEqual(recovery.state['tokens'], 1510)
        restored = PlanningRecovery(session, recovery.phase, 3, True, initial_tokens=147545)
        self.assertEqual(restored.state['tokens'], 147545)
        self.assertEqual(restored.state['calls'], 2)

    async def test_budget_preflight_uses_trimmed_input_window(self):
        session = AgentSession(self.root)
        phase = session.phase('planning', 'test', [])
        messages = [{'role': 'user', 'content': '中' * 10000}]
        seen = []
        class Gateway:
            async def chat(self, client, state, messages, tools, max_tokens):
                seen.append((copy.deepcopy(messages), max_tokens))
                return {'content': 'OK'}
        with patch.dict('os.environ', {'AGENT_PLANNING_MAX_TOKENS': '2000'}), \
                patch.object(session, 'phase_window', return_value=[{'role': 'user', 'content': 'trimmed'}]):
            recovery = PlanningRecovery(session, phase, 3, True)
            await phase_chat(session, Gateway(), None, AgentState.PLAN, messages, [],
                             'planning', phase, 4096, before_call=recovery.before_call)
        self.assertEqual(seen[0][0][0]['content'], 'trimmed')
        self.assertEqual(seen[0][1], 2000 - recovery.input_tokens)

    def test_patch_draft_remains_retrievable_after_message_window_changes(self):
        session = AgentSession(self.root)
        recovery = PlanningRecovery(session, session.phase('planning', 'test', []), 3, True)
        recovery.candidate(json.dumps(self.plan))
        recovery.candidate('{"plan_patch":{"design":"Revised design"}}')
        phase = recovery.phase
        session.save_phase('planning', phase)
        restored = AgentSession(self.root)
        output = restored.read_output(phase['candidate_output_id'], 0, 12000)
        self.assertIn('Revised design', output)
        self.assertIn('Print hello', output)
        self.assertEqual(phase['candidate_hash'], digest(phase['candidate']))

    async def test_legacy_failed_transcript_keeps_candidate_when_context_changes(self):
        invalid = self.invalid_paths()
        session = AgentSession(self.root)
        session.save_phase('planning', {'key': 'legacy', 'completed': False, 'request': 'Add shared persistence',
            'synthesis': True, 'rounds': 5, 'last_error': 'backend/app/db.py missing', 'observations': {},
            'messages': [{'role': 'assistant', 'content': json.dumps(invalid)}]})
        corrected = copy.deepcopy(invalid['change_map'])
        for entry in corrected[1:]: entry['operation'] = 'create'
        result, observed = await self.run_planner([{'content': json.dumps({'plan_patch': {'change_map': corrected}})}], resume_planning=True)
        self.assertTrue(observed[0]['tools'])
        self.assertEqual(result['goal'], invalid['goal'])
        self.assertIn('未经当前源码校验', json.dumps(observed[0]['messages'], ensure_ascii=False))
        self.assertIn('增量工作流程', json.dumps(observed[0]['messages'], ensure_ascii=False))
        self.assertIn('当前模块入口概览', json.dumps(observed[0]['messages'], ensure_ascii=False))

    def test_merge_patch_retains_valid_sections_and_does_not_mutate_base(self):
        before = copy.deepcopy(self.plan)
        result = merge_patch(self.plan, {'commands': {'test': ['python check.py']}, 'unused': None})
        self.assertEqual(self.plan, before)
        self.assertEqual(result['commands']['build'], [])
        self.assertEqual(result['commands']['test'], ['python check.py'])
        self.assertEqual(result['requirements'], before['requirements'])

    def test_patch_without_candidate_and_bad_envelope_require_correction(self):
        session = AgentSession(self.root)
        recovery = PlanningRecovery(session, session.phase('planning', 'test', []), 3, True)
        for text in ['{"plan_patch":{"goal":"new"}}', '{"plan_patch":[],"goal":"new"}']:
            with self.assertRaises(ValueError): recovery.candidate(text)
