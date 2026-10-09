"""Correctness and request-volume regressions for the whole Agent pipeline."""
import copy
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from agent import make_plan, run_agent, write_file, write_files, list_files, snapshot_files
from agent_harness import TaskLedger
from agent_session import AgentSession, digest, encoded, estimate_tokens, inventory, repair_tool_boundaries
from coding_runtime import AgentState
from test_agent_execution import cli_plan, calls


class EfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / uuid.uuid4().hex
        self.root.mkdir(); (self.root / '.atoms').mkdir()
        self.plan = cli_plan()
        self.ledger = TaskLedger(self.root, self.plan, 'Create CLI')
        self.session = AgentSession(self.root)
        self.prefix = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'Create CLI'}]
        self.session.start('Create CLI', self.plan, 'model', 'system', self.prefix, {})

    def test_installed_python_dependencies_are_not_project_context(self):
        package = self.root / '.local/lib/python/site-packages/vendor'
        package.mkdir(parents=True)
        (package / 'dependency.py').write_text('not project source')
        (self.root / 'greet.py').write_text('project source')
        self.assertNotIn('.local/lib/python/site-packages/vendor/dependency.py', list_files(self.root))
        self.assertEqual(inventory(snapshot_files(self.root)), inventory({'greet.py': 'project source'}))

    def test_long_output_is_losslessly_available_after_restart(self):
        raw = 'verification_id=V1\nexit_code=1\n' + 'log ' * 15000 + 'ROOT CAUSE: missing dependency'
        preview = self.session.output(calls(('run_shell', {'command': 'test'}))['tool_calls'][0], raw)
        self.assertLess(len(preview), 4300)
        self.assertIn('exit_code=1', preview)
        self.assertIn('ROOT CAUSE', preview)
        identifier = preview.splitlines()[0].split('=')[1]
        restored = AgentSession(self.root)
        parts = [restored.read_output(identifier, i, 12000).split('\n', 1)[1] for i in range(0, len(raw), 12000)]
        self.assertEqual(''.join(parts), raw)
        with self.assertRaises(ValueError): restored.read_output(identifier, -1)
        with self.assertRaises(ValueError): restored.read_output('not-owned')

    def test_pagination_does_not_create_recursive_artifact_references(self):
        preview = self.session.output(calls(('run_shell', {'command': 'test'}))['tool_calls'][0], 'log ' * 5000)
        identifier = preview.splitlines()[0].split('=')[1]
        result = self.session.read_output(identifier, 0, 500)
        returned = self.session.output(calls(('read_tool_output', {'output_id': identifier}))['tool_calls'][0], result)
        self.assertEqual(returned, result)
        with self.session.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM outputs').fetchone()[0], 1)

    def test_identical_writes_do_not_touch_files_and_model_can_reuse_own_write(self):
        with patch('agent.project_uid', return_value=os.getuid()):
            write_file(self.root, 'greet.py', 'print("hello")')
            timestamp = (self.root / 'greet.py').stat().st_mtime_ns
            self.assertIn('未重复写入', write_file(self.root, 'greet.py', 'print("hello")'))
        self.assertEqual(timestamp, (self.root / 'greet.py').stat().st_mtime_ns)
        messages = self.prefix + [calls(('write_file', {'path': 'greet.py', 'content': 'print("hello")'})) | {'role': 'assistant'}]
        self.assertIn('没有变化', self.session.read('greet.py', 'print("hello")', 0, 12000, messages))
        self.assertIn('changed', self.session.read('greet.py', 'print("changed")', 0, 12000, messages))

    def test_pruning_reduces_repeated_code_without_losing_pairs_or_full_results(self):
        messages = copy.deepcopy(self.prefix)
        for i in range(12):
            call = calls(('write_file', {'path': f'f{i}.py', 'content': 'code ' * 2500}))
            call['tool_calls'][0]['id'] = str(i)
            messages += [call | {'role': 'assistant', 'reasoning_details': [{'type': 'reasoning.encrypted', 'data': 'opaque'}]},
                         {'role': 'tool', 'tool_call_id': str(i), 'content': f'verification_id=V{i}\n' + 'source ' * 1000}]
        self.session.checkpoint(messages, self.ledger, [])
        before = estimate_tokens(messages)
        reduced = self.session.prune(messages, 2, 20000)
        self.assertLess(estimate_tokens(reduced), before * 0.5)
        self.assertEqual(reduced[:2], messages[:2])
        self.assertEqual(repair_tool_boundaries(reduced), reduced)
        self.assertNotIn('opaque', encoded(reduced))
        self.assertIn('f11.py', encoded(reduced))
        archived = [m for m in reduced if m.get('role') == 'tool' and m['content'].startswith('output_id=')]
        identifier = archived[0]['content'].splitlines()[0].split('=')[1]
        self.assertIn('source ', self.session.read_output(identifier))
        with self.session.connect() as c:
            self.assertIn('code code', '\n'.join(r[0] for r in c.execute('SELECT data FROM events')))

    def test_task_updates_do_not_echo_whole_requirements_and_evidence(self):
        evidence = self.ledger.record('run_shell', 'python greet.py', 0, 'hello', 'sha', ['R1'])
        output = self.ledger.update('T1', 'done', 'verified', [evidence])
        self.assertIn('updated_task', output)
        self.assertNotIn('architecture', output)
        self.assertIn(evidence, self.ledger.model_context())
        self.assertEqual(self.ledger.tasks[0]['status'], 'done')

    def test_current_requirement_proof_survives_more_than_twelve_failed_setup_calls(self):
        from agent_harness import source_digest
        files = {'greet.py': 'print("hello")'}
        current = source_digest(files)
        receipt = self.ledger.record('run_shell', 'python greet.py', 0, 'hello', current, ['R1'])
        old = self.ledger.record('run_shell', 'old', 0, 'old', 'old-source', ['R1'])
        for i in range(15):
            self.ledger.record('browser_check', str(i), 1, 'selector mismatch', current, [])
        context = json.loads(self.ledger.model_context(files))
        proofs = {e['id']: e for e in context['evidence']}
        self.assertIn(receipt, proofs)
        self.assertTrue(proofs[receipt]['current_source'])
        self.assertNotIn(old, proofs)
        self.assertIn('completion_issues', context)

    def test_batch_validates_all_paths_before_writing(self):
        with self.assertRaises(ValueError):
            write_files(self.root, [{'path': 'ok.py', 'content': 'ok'}, {'path': '../outside.py', 'content': 'bad'}])
        self.assertFalse((self.root / 'ok.py').exists())
        files = [{'path': 'greet.py', 'content': 'hello'}, {'path': 'tests/a.py', 'content': 'test'}]
        with patch('agent.project_uid', return_value=os.getuid()):
            self.assertIn('tests/a.py', write_files(self.root, files))
        messages = self.prefix + [calls(('write_files', {'files': files})) | {'role': 'assistant'}]
        self.assertIn('没有变化', self.session.read('greet.py', 'hello', 0, 12000, messages))

    def test_phase_window_preserves_user_corrections_and_tool_receipts(self):
        messages = copy.deepcopy(self.prefix)
        messages.append({'role': 'user', 'content': 'Keep the public API and all error paths unchanged.'})
        for i in range(6):
            call = calls(('read_file', {'path': f'f{i}.py'}))
            call['tool_calls'][0]['id'] = str(i)
            result = self.session.output(call['tool_calls'][0], 'verified source ' * 600)
            messages += [call | {'role': 'assistant'}, {'role': 'tool', 'tool_call_id': str(i), 'content': result}]
        reduced = self.session.phase_window(messages, 10000)
        self.assertLess(estimate_tokens(reduced), estimate_tokens(messages))
        self.assertIn('Keep the public API and all error paths unchanged.', encoded(reduced))
        self.assertIn('output_id=', encoded(reduced))
        self.assertEqual(repair_tool_boundaries(reduced), reduced)


class PlanningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        catalog = patch('model_catalog.catalog', return_value=[{'id': 'model', 'context': 128000}])
        catalog.start()
        self.addCleanup(catalog.stop)

    async def test_verified_functional_acceptance_completes_without_source_review_model_calls(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / uuid.uuid4().hex
            root.mkdir(); (root / '.atoms').mkdir()
            sequence = [calls(('write_file', {'path': 'greet.py', 'content': 'print("hello")'}),
                              ('write_file', {'path': 'README.md', 'content': 'Run python greet.py. Local CLI; no backend or persistence.'})),
                        calls(('run_shell', {'command': 'python greet.py', 'requirement_ids': ['R1']})),
                        calls(('update_task', {'id': 'T1', 'status': 'done', 'evidence_ids': ['V1']})),
                        {'content': 'Implemented and verified'}]
            reviews = []
            class Gateway:
                def __init__(self, *args): pass
                async def chat(self, client, stage, messages, tools=None, **kwargs):
                    if stage == AgentState.REVIEW:
                        reviews.append(tools)
                        raise AssertionError('Functional acceptance must not start a source-reading review loop')
                    if not sequence:
                        raise AssertionError(str(messages[-1]))
                    return sequence.pop(0)
            with patch('agent.ensure_workspace', return_value=root), patch('agent.project_uid', return_value=os.getuid()), patch('agent.ModelGateway', Gateway):
                await run_agent(uuid.UUID(hex=root.name), 'Create CLI', 'model', lambda *a, **k: None, plan=json.dumps(cli_plan()))
            self.assertEqual(reviews, [])
            self.assertEqual(AgentSession(root).state['acceptance']['mode'], 'runtime_and_core_workflows')
            ledger = json.loads((root / '.atoms/task-state.json').read_text())
            self.assertTrue(ledger['completed'])
            self.assertEqual(ledger['evidence'][0]['exit_code'], 0)

    async def test_single_tool_loop_handoff_reuses_reads_and_invalidates_changed_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / uuid.uuid4().hex
            root.mkdir(); (root / '.atoms').mkdir()
            (root / 'greet.py').write_text('print("hello")')
            count = []
            sequence = [calls(('read_file', {'path': 'greet.py'})), {'content': json.dumps(cli_plan())}]
            class Gateway:
                def __init__(self, *args): pass
                async def chat(self, client, stage, messages, *args, **kwargs):
                    count.append(stage)
                    return sequence.pop(0)
            with patch('agent.ensure_workspace', return_value=root), patch('agent.project_uid', return_value=os.getuid()), patch('agent.ModelGateway', Gateway):
                planned = json.loads(await make_plan(uuid.UUID(hex=root.name), 'Create CLI', 'model'))
                # Same planning checkpoint: no extra model call.
                await make_plan(uuid.UUID(hex=root.name), 'Create CLI', 'model')
            self.assertEqual(count, [AgentState.PLAN, AgentState.PLAN])
            session = AgentSession(root)
            handoff = session.planning_handoff('Create CLI', planned, {'greet.py': 'print("hello")'})
            self.assertIn('print("hello")', handoff)
            changed = session.planning_handoff('Create CLI', planned, {'greet.py': 'print("HELLO")'})
            self.assertNotIn('print("hello")', changed)
            self.assertIn('需重新读取', changed)
