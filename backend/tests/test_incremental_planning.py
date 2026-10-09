import copy
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from agent import make_plan, run_agent
from agent_session import AgentSession, encoded
from incremental_planning import assemble_plan, baseline_plan, verified_observations, project_outline, plan_object
from test_agent_execution import cli_plan, calls


class IncrementalPlanningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / '.atoms').mkdir()
        self.plan = cli_plan()
        self.plan['change_map'] = [{'path': 'greet.py', 'operation': 'modify', 'reason': 'Add the requested CLI behavior', 'requirement_ids': ['R1']}]
        catalog = patch('model_catalog.catalog', return_value=[{'id': 'model', 'context': 128000}])
        catalog.start()
        self.addCleanup(catalog.stop)
        (self.root / 'greet.py').write_text('print("hello")')
        (self.root / '.atoms/task-state.json').write_text(json.dumps({'plan': self.plan}))

    def test_delta_inherits_stack_commands_and_preserves_regression_requirements(self):
        delta = copy.deepcopy(self.plan)
        for key in ('application_type', 'architecture', 'commands'):
            delta.pop(key)
        delta['requirements'][0]['description'] = 'Print goodbye'
        base = copy.deepcopy(self.plan)
        base['commands']['bootstrap'] = ['create-project']
        result = assemble_plan(delta, base)
        self.assertEqual(result['architecture'], base['architecture'])
        self.assertEqual(result['commands']['bootstrap'], [])
        self.assertEqual(result['preserve_requirements'], base['requirements'])
        self.assertNotIn('preserve_requirements', delta)
        self.assertEqual(base['commands']['bootstrap'], ['create-project'])

    def test_explicit_architecture_change_survives_and_invalid_new_requirements_fail(self):
        delta = copy.deepcopy(self.plan)
        delta['architecture']['backend']['required'] = True
        delta['architecture']['backend']['stack'] = 'FastAPI'
        self.assertTrue(assemble_plan(delta, self.plan)['architecture']['backend']['required'])
        delta['requirements'] = []
        with self.assertRaises(ValueError):
            assemble_plan(delta, self.plan)

    def test_baseline_requires_existing_source_and_valid_plan(self):
        session = AgentSession(self.root)
        self.assertIsNone(baseline_plan(self.root, session, {'.atoms/README.md': 'metadata'}))
        self.assertEqual(baseline_plan(self.root, session, {'greet.py': 'hello'})['goal'], self.plan['goal'])

    def test_source_cache_hash_validation_excludes_changed_and_deleted_files(self):
        session = AgentSession(self.root)
        for name in ('stable.py', 'changed.py', 'deleted.py'):
            session.read(name, 'old', 0, 6000, [])
        selected = verified_observations(session, {'stable.py': 'old', 'changed.py': 'new'})
        self.assertEqual(list(selected), ['stable.py'])
        self.assertIn('old', selected['stable.py']['text'])

    def test_outline_uses_current_syntax_without_source_bodies_or_test_noise(self):
        outline = project_outline({'backend/app/routes.py': '@router.post("/register")\ndef register(payload):\n    secret = "private-value"\n',
                                   'frontend/src/types.ts': 'export interface User { id: number }',
                                   'backend/tests/test_auth.py': 'def noisy_test(): pass'})
        self.assertIn('POST /register', outline)
        self.assertIn('User', outline)
        self.assertNotIn('private-value', outline)
        self.assertNotIn('noisy_test', outline)
        self.assertIn('GET /me', project_outline({'backend/app/routes.py': '@router.get("/me")\ndef me(): pass'}))

    def test_incomplete_outer_plan_is_not_parsed_as_nested_architecture(self):
        with self.assertRaisesRegex(ValueError, '语法错误'):
            plan_object('{"goal":"new feature","architecture":{"backend":{}},"tasks":[')
        self.assertEqual(plan_object('{"goal":"line one\nline two"}')['goal'], 'line one\nline two')

    def test_new_request_keeps_project_identity_but_not_old_execution_or_billing_state(self):
        session = AgentSession(self.root)
        session.start('original', self.plan, 'model', 'system',
                      [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'original'}], {'greet.py': 'old'})
        identity = session.state['session_id']
        session.state['summary'] = 'Old implementation is verified'
        session.state['pending_requests'] = {'IMPLEMENT': {'id': 'old-request'}}
        session.start('new feature', self.plan, 'model', 'system',
                      [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'new feature'}], {'greet.py': 'old'})
        self.assertEqual(session.state['session_id'], identity)
        self.assertEqual(session.state['original_request'], 'original')
        self.assertEqual(session.state['pending_requests'], {})
        self.assertEqual(session.state['previous_summary'], 'Old implementation is verified')
        self.assertEqual(session.state['plan'], self.plan)

    def test_phase_compression_retains_source_tail_and_retrievable_full_output(self):
        session = AgentSession(self.root)
        source = 'imports\n' + 'body\n' * 4000 + '\nimportant_route_contract'
        messages = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'request'}]
        for i in range(5):
            call = calls(('read_file', {'path': f'file{i}.py'}))
            messages.extend([{'role': 'assistant', **call}, {'role': 'tool', 'tool_call_id': 'call0', 'content': source}])
        window = session.phase_window(messages, 5000, force=True)
        self.assertIn('important_route_contract', window[2]['content'])
        self.assertIn('output_id=', window[2]['content'])
        with session.connect() as conn:
            row = conn.execute("SELECT id FROM outputs WHERE tool='archived_phase' LIMIT 1").fetchone()
        self.assertIn('body', session.read_output(row[0]))

    async def test_repeated_reads_cannot_consume_synthesis_turns(self):
        observed = []
        delta = copy.deepcopy(self.plan)
        for key in ('application_type', 'architecture', 'commands'):
            delta.pop(key)

        class Gateway:
            def __init__(self, *args): pass
            async def chat(self, client, state, messages, tools=None, max_tokens=0):
                observed.append(copy.deepcopy(tools))
                return calls(('read_file', {'path': 'greet.py'})) if tools else {'content': json.dumps(delta)}

        with patch('agent.ensure_workspace', return_value=self.root), patch('agent.project_uid', return_value=os.getuid()), patch('agent.ModelGateway', Gateway):
            result = json.loads(await make_plan(uuid.uuid4(), 'Add goodbye', 'model'))
        self.assertEqual(len(observed), 4)
        self.assertEqual(observed[-1], [])
        self.assertEqual(result['development_mode'], 'incremental')

    async def test_new_prompt_navigation_handoff_patch_and_real_flow_verification(self):
        self.root.joinpath('greet.py').write_text('def greet():\n    return "hello"\n\nprint(greet())\n')
        self.root.joinpath('README.md').write_text('Run python greet.py; existing Python CLI.\n')
        delta = copy.deepcopy(self.plan)
        delta['goal'] = 'Add goodbye while preserving hello'
        delta['requirements'][0].update(description='Also print goodbye', acceptance=['CLI prints both hello and goodbye'])
        delta['tasks'][0]['verification'] = 'python greet.py'
        sequence = [calls(('locate_change', {'queries': ['greet']})),
                    calls(('read_code', {'path': 'greet.py', 'symbol': 'greet', 'max_lines': 2})),
                    {'content': json.dumps(delta)},
                    calls(('search_code', {'query': 'print(greet())', 'include': '*.py'})),
                    calls(('replace_in_file', {'path': 'greet.py', 'old': 'print(greet())', 'new': 'print(greet())\nprint("goodbye")'})),
                    calls(('run_shell', {'command': 'python -c \'import subprocess; out=subprocess.check_output(["python", "greet.py"],text=True); assert out=="hello\\ngoodbye\\n"; print(out)\'', 'requirement_ids': ['R1']})),
                    calls(('update_task', {'id': 'T1', 'status': 'done', 'evidence_ids': ['V1']})),
                    {'content': 'Added goodbye and verified both new and existing CLI behavior.'}]
        seen_execution = []
        class Gateway:
            def __init__(self, *args): pass
            async def chat(self, client, state, messages, tools=None, max_tokens=0):
                if state.value != 'PLAN':
                    seen_execution.append(copy.deepcopy(messages))
                return sequence.pop(0)
        with patch('agent.ensure_workspace', return_value=self.root), patch('agent.project_uid', return_value=os.getuid()), patch('agent.ModelGateway', Gateway):
            project = uuid.uuid4()
            plan = await make_plan(project, 'Add goodbye while preserving hello', 'model')
            result = await run_agent(project, 'Add goodbye while preserving hello', 'model', lambda *args, **kwargs: None, plan=plan)
        self.assertFalse(sequence)
        self.assertIn('goodbye', result['summary'])
        self.assertIn('已定位的需求改动', encoded(seen_execution[0]))
        self.assertIn('return \\"hello\\"', encoded(seen_execution[0]))
        self.assertEqual((self.root / 'greet.py').read_text().count('def greet'), 1)

    async def test_invalid_json_repair_uses_no_tools_and_retains_new_goal(self):
        observed = []
        repaired = copy.deepcopy(self.plan)
        repaired['goal'] = 'New business feature'

        class Gateway:
            def __init__(self, *args): pass
            async def chat(self, client, state, messages, tools=None, max_tokens=0):
                observed.append(copy.deepcopy(tools))
                return {'content': 'analysis only'} if len(observed) == 1 else {'content': json.dumps(repaired)}

        with patch('agent.ensure_workspace', return_value=self.root), patch('agent.project_uid', return_value=os.getuid()), patch('agent.ModelGateway', Gateway):
            result = json.loads(await make_plan(uuid.uuid4(), 'New business feature', 'model'))
        self.assertEqual(observed[-1], [])
        self.assertEqual(result['goal'], 'New business feature')

    async def test_reasoning_starvation_reserves_visible_plan_output_and_never_accepts_truncation(self):
        observed = []
        plan = copy.deepcopy(self.plan)

        class Gateway:
            base_url = 'https://openrouter.ai/api/v1'
            def __init__(self, *args): pass
            async def chat(self, client, state, messages, tools=None, max_tokens=0, **kwargs):
                observed.append({'tools': copy.deepcopy(tools), 'max_tokens': max_tokens, **kwargs})
                if len(observed) == 1:
                    return {'content': json.dumps(plan), '_response_meta': {'finish_reason': 'length', 'reasoning_tokens': 7900}}
                return {'content': json.dumps(plan), '_response_meta': {'finish_reason': 'stop'}}

        with patch('agent.ensure_workspace', return_value=self.root), patch('agent.project_uid', return_value=os.getuid()), patch('agent.ModelGateway', Gateway):
            await make_plan(uuid.uuid4(), 'Add feature', 'model')
        self.assertEqual(len(observed), 2)
        self.assertEqual(observed[1]['tools'], [])
        self.assertEqual(observed[1]['max_tokens'], 16000)
        self.assertEqual(observed[1]['reasoning'], {'effort': 'low'})
