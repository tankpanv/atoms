import copy
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from agent import run_agent, safe_file, snapshot_files
from agent_harness import TaskLedger, resumable_plan
from agent_session import AgentSession, encoded, estimate_tokens, repair_tool_boundaries, session_status
from coding_runtime import AgentState
from test_agent_execution import cli_plan, calls


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.plan = cli_plan()
        self.files = {'greet.py': 'print("hello")', 'README.md': 'Run CLI'}
        self.ledger = TaskLedger(self.root, self.plan, 'Create CLI')
        self.prefix = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'Create CLI'}]

    def start(self, session=None, files=None, model='model'):
        session = session or AgentSession(self.root)
        messages, prefix = session.start('Create CLI', self.plan, model, 'system', copy.deepcopy(self.prefix), files or self.files)
        return session, messages, prefix

    def test_preview_type_is_available_before_session_and_survives_corruption(self):
        path = self.root / '.atoms/task-state.json'
        for kind in ('artifact', 'web', 'service', 'cli', 'library'):
            path.write_text(json.dumps({'plan': {'application_type': kind}}))
            self.assertEqual(session_status(self.root), {'available': False, 'application_type': kind})
        path.write_text(json.dumps({'plan': {'application_type': 'artifact'}}))
        session, _, _ = self.start()
        session.path.write_bytes(b'invalid sqlite checkpoint')
        self.assertEqual(session_status(self.root), {'available': False, 'application_type': 'artifact'})
        path.write_text('invalid json')
        self.assertEqual(session_status(self.root), {'available': False})

    def test_sqlite_reopen_restores_exact_context_and_request_identity(self):
        session, messages, _ = self.start()
        messages += [calls(('read_file', {'path': 'greet.py'})) | {'role': 'assistant'},
                     {'role': 'tool', 'tool_call_id': 'call0', 'content': 'print("hello")'}]
        session.checkpoint(messages, self.ledger, [{'tool': 'read_file'}], self.files)
        payload = {'model': 'model', 'messages': messages}
        identifier = session.request_id(payload, 'IMPLEMENT', 'job')
        restored, window, _ = self.start(AgentSession(self.root))
        self.assertTrue(restored.restored)
        self.assertEqual(window, messages)
        self.assertEqual(restored.request_id(payload, 'IMPLEMENT', 'job'), identifier)
        self.assertNotEqual(restored.request_id(payload, 'IMPLEMENT', 'new-job'), identifier)

    def test_file_cache_survives_restart_but_same_size_edits_and_deletions_invalidate(self):
        session, messages, _ = self.start()
        result = session.read('greet.py', 'print("hello")', 0, 12000, messages)
        messages += [calls(('read_file', {'path': 'greet.py'})) | {'role': 'assistant'},
                     {'role': 'tool', 'tool_call_id': 'call0', 'content': result}]
        session.checkpoint(messages, self.ledger, [], self.files)
        restored, window, _ = self.start(AgentSession(self.root))
        hit = restored.read('greet.py', 'print("hello")', 0, 12000, window)
        self.assertIn('没有变化', hit)
        changed = {'greet.py': 'print("HELLO")'}
        restored, window, _ = self.start(AgentSession(self.root), changed)
        self.assertEqual(restored.changed, ['README.md', 'greet.py'])
        self.assertIn('旧内容已失效', window[-1]['content'])
        self.assertIn('HELLO', restored.read('greet.py', changed['greet.py'], 0, 12000, window))

    def test_unfinished_tool_batch_is_not_replayed_and_pairs_are_valid(self):
        session, messages, _ = self.start()
        messages += [calls(('write_file', {'path': 'greet.py'}), ('run_shell', {'command': 'dangerous'})) | {'role': 'assistant'},
                     {'role': 'tool', 'tool_call_id': 'call0', 'content': 'written'}]
        session.checkpoint(messages, self.ledger, [], self.files)
        _, restored, _ = self.start(AgentSession(self.root))
        self.assertEqual(restored[-2]['content'], 'written')
        self.assertIn('不要自动重复', restored[-1]['content'])
        self.assertEqual([m['tool_call_id'] for m in restored if m['role'] == 'tool'], ['call0', 'call1'])
        self.assertEqual(repair_tool_boundaries(restored), restored)

    def test_model_switch_and_compaction_drop_nonportable_reasoning_and_archive_history(self):
        session, messages, prefix = self.start()
        for i in range(8):
            messages += [{'role': 'assistant', 'content': 'decision-' + str(i),
                          'reasoning_details': [{'type': 'reasoning.encrypted', 'data': 'sealed'}],
                          'tool_calls': [{'id': str(i), 'function': {'name': 'read_file', 'arguments': '{}'}}]},
                         {'role': 'tool', 'tool_call_id': str(i), 'content': 'large source ' * 800}]
        session.checkpoint(messages, self.ledger, [], self.files)
        before = estimate_tokens(messages)
        reduced = session.compact(messages, prefix, self.ledger, [], 'Keep Python CLI; next run verification.')
        self.assertLess(estimate_tokens(reduced), before)
        self.assertIn('R1', encoded(reduced))
        self.assertNotIn('reasoning.encrypted', encoded(reduced))
        with session.connect() as conn:
            archive = '\n'.join(row[0] for row in conn.execute('SELECT data FROM events'))
        self.assertIn('decision-0', archive)
        self.assertIn('large source', archive)
        restored, changed, _ = self.start(AgentSession(self.root), model='another-model')
        self.assertTrue(restored.restored)
        self.assertNotIn('sealed', encoded(changed))
        self.assertIn('模型或专家指令已更新', changed[-1]['content'])

    def test_new_requirement_uses_memory_without_reusing_the_old_plan(self):
        session, messages, _ = self.start()
        session.state['summary'] = 'Uses Python CLI with greet.py; preserve it.'
        session.checkpoint(messages, self.ledger, [{'tool': 'write_file', 'input': {'path': 'greet.py'}}], self.files)
        restored = AgentSession(self.root)
        memory = restored.planning_context({**self.files, 'new.py': 'new'})
        self.assertIn('Uses Python CLI', memory)
        self.assertIn('new.py', memory)
        self.assertIsNone(resumable_plan(self.root, '继续，并增加登录'))
        self.assertIsNotNone(resumable_plan(self.root, '继续上次任务'))

    def test_private_session_storage_cannot_be_read_or_published_as_source(self):
        self.start()
        with self.assertRaises(ValueError):
            safe_file(self.root, '.atoms/.agent-session/session.sqlite3')
        self.assertFalse(any('.agent-session' in name for name in snapshot_files(self.root)))
        with tempfile.TemporaryDirectory() as other:
            other_root = Path(other)
            (other_root / '.atoms').mkdir()
            (other_root / '.atoms/.agent-session').symlink_to(self.root / '.atoms/.agent-session')
            with self.assertRaises(ValueError):
                AgentSession(other_root)

    def test_binary_assets_are_tracked_without_being_treated_as_text(self):
        assets = {**self.files, 'hero.png': {'encoding': 'base64', 'content': 'iVBORw=='}}
        session, _, _ = self.start(files=assets)
        self.assertIn('hero.png', session.state['files'])
        _, messages, _ = self.start(AgentSession(self.root), {**self.files, 'hero.png': {'encoding': 'base64', 'content': 'bmV3'}})
        self.assertIn('hero.png', messages[-1]['content'])


class ResumeExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        catalog = patch('model_catalog.catalog', return_value=[{'id': 'test-model', 'context': 128000}])
        catalog.start()
        self.addCleanup(catalog.stop)

    async def test_real_file_tools_resume_without_replanning_or_retrieval(self):
        phase = [1]
        observed = []
        sequence = [calls(('write_file', {'path': 'greet.py', 'content': 'print("hello")\n'}),
                          ('write_file', {'path': 'README.md', 'content': 'Run python greet.py'}),
                          ('read_file', {'path': 'greet.py'}))]
        class Gateway:
            def __init__(self, *args): pass
            async def chat(self, client, state, messages, tools=None, max_tokens=0):
                if phase[0] == 1:
                    if sequence:
                        return sequence.pop(0)
                    raise RuntimeError('simulated service restart after committed tools')
                observed.append(copy.deepcopy(messages))
                if state == AgentState.REVIEW:
                    return {'content': '{"approved":true,"issues":[],"summary":"Verified CLI"}'}
                return sequence.pop(0)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / uuid.uuid4().hex
            root.mkdir()
            steps = []
            with patch('agent.ensure_workspace', return_value=root), patch('agent.project_uid', return_value=os.getuid()), patch('agent.ModelGateway', Gateway):
                with self.assertRaisesRegex(RuntimeError, 'simulated service restart'):
                    await run_agent(uuid.UUID(root.name), 'Create CLI', 'test-model', lambda *args, **kwargs: None, plan=json.dumps(cli_plan()))
                self.assertEqual((root/'greet.py').read_text(), 'print("hello")\n')
                phase[0] = 2
                sequence.extend([{'content': 'Inspecting saved implementation before choosing tools.'},
                                 calls(('run_shell', {'command': 'python greet.py', 'requirement_ids': ['R1']})),
                                 calls(('update_task', {'id': 'T1', 'status': 'done', 'evidence_ids': ['V1']})),
                                 {'content': 'Resumed CLI verified.'}])
                with patch('agent.retrieve_context', side_effect=AssertionError('resume must not retrieve source again')):
                    result = await run_agent(uuid.UUID(root.name), 'Create CLI', 'test-model',
                                             lambda kind, label, detail, **kwargs: steps.append(label), plan=json.dumps(cli_plan()))
            self.assertIn('已恢复构建会话', steps)
            self.assertIn('文件版本', encoded(observed[0]))
            self.assertEqual(sum(m.get('role') == 'tool' and '已写入 greet.py' in m.get('content', '') for m in observed[0]), 1)
            self.assertIn('hello', AgentSession(root).state['acceptance']['report'])
            self.assertNotIn('Inspecting saved implementation', result['summary'])
            self.assertTrue(AgentSession(root).state['completed'])
