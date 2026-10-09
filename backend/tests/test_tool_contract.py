import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from agent import TOOLS, run_agent, write_files, read_file, write_file
from coding_runtime import AgentState, apply_unified_patch
from tool_contract import ToolArgumentError, parse_tool_arguments
from tool_limits import SHELL_TIMEOUT_MAX, READ_LIMIT_MAX, WRITE_BATCH_MAX
from test_agent_execution import calls, cli_plan


class ContractTests(unittest.TestCase):
    def test_execution_hints_are_capped_without_mutating_model_request(self):
        for name, args, field, effective in [
            ('run_shell', {'command': 'test', 'timeout': 300}, 'timeout', SHELL_TIMEOUT_MAX),
            ('read_file', {'path': 'x', 'limit': 45000}, 'limit', READ_LIMIT_MAX),
            ('read_document', {'id': '00000000-0000-0000-0000-000000000001', 'limit': 20000}, 'limit', READ_LIMIT_MAX),
            ('read_tool_output', {'output_id': 'x', 'limit': 16000}, 'limit', READ_LIMIT_MAX)]:
            call = calls((name, args))['tool_calls'][0]
            adjustments = []
            parsed = parse_tool_arguments(call, TOOLS, adjustments=adjustments)
            self.assertEqual(parsed[field], effective)
            self.assertEqual(json.loads(call['function']['arguments'])[field], args[field])
            self.assertEqual(adjustments, [{'field': field, 'requested': args[field], 'effective': effective}])
        for timeout in (-1, 0, True, '300'):
            with self.assertRaises(ToolArgumentError):
                parse_tool_arguments(calls(('run_shell', {'command': 'test', 'timeout': timeout}))['tool_calls'][0], TOOLS)

    def test_complete_batches_are_not_rejected_by_recommended_size(self):
        files = [{'path': f'{i}.py', 'content': 'print(1)'} for i in range(WRITE_BATCH_MAX)]
        parsed = parse_tool_arguments(calls(('write_files', {'files': files}))['tool_calls'][0], TOOLS)
        self.assertEqual(parsed['files'], files)
        files.append({'path': 'extra', 'content': 'x'})
        with self.assertRaises(ToolArgumentError):
            parse_tool_arguments(calls(('write_files', {'files': files}))['tool_calls'][0], TOOLS)

    def test_recovery_instructions_match_actual_tool(self):
        for name, args in [('run_shell', {'command': 123}), ('read_file', {}), ('update_task', {})]:
            with self.assertRaises(ToolArgumentError) as cm:
                parse_tool_arguments(calls((name, args))['tool_calls'][0], TOOLS)
            result = json.loads(cm.exception.result())
            self.assertNotIn('write_files', result['recovery'])
            self.assertFalse(result['executed'])

    def test_browser_actions_are_checked_before_starting_runtime(self):
        for action in [{'action': 'click'}, {'action': 'fill', 'selector': 'input'},
                       {'action': 'assert_count', 'value': '>=2'}, {'action': 'press', 'selector': 'input', 'value': 5}]:
            with self.assertRaises(ToolArgumentError):
                parse_tool_arguments(calls(('browser_check', {'actions': [action]}))['tool_calls'][0], TOOLS)
        args = {'actions': [{'action': 'reload'}, {'action': 'assert_text', 'value': 'hello'}, {'action': 'assert_count', 'value': 2}]}
        self.assertEqual(parse_tool_arguments(calls(('browser_check', args))['tool_calls'][0], TOOLS), args)

    def test_task_completion_requires_evidence_before_handler(self):
        for args in [{'id': 'T1', 'status': 'done'}, {'id': 'T1', 'status': 'done', 'evidence_ids': []}]:
            with self.assertRaises(ToolArgumentError) as cm:
                parse_tool_arguments(calls(('update_task', args))['tool_calls'][0], TOOLS)
            self.assertEqual(cm.exception.code, 'MISSING_ARGUMENT')
            self.assertIn('get_tasks', json.loads(cm.exception.result())['recovery'])
        args = {'id': 'T1', 'status': 'in_progress'}
        self.assertEqual(parse_tool_arguments(calls(('update_task', args))['tool_calls'][0], TOOLS), args)

    def test_truncated_json_with_misleading_finish_label_is_not_executable(self):
        call = calls(('write_files', {}))['tool_calls'][0]
        call['function']['arguments'] = '{"files":[{"path":"large.css","content":"abc'
        with self.assertRaises(ToolArgumentError) as cm:
            parse_tool_arguments(call, TOOLS, {'_response_meta': {'finish_reason': 'tool_calls', 'completion_tokens': 12000, 'max_tokens': 12000}})
        self.assertEqual(cm.exception.code, 'OUTPUT_TRUNCATED')
        self.assertFalse(json.loads(cm.exception.result())['executed'])

    def test_length_response_blocks_even_a_syntactically_complete_command(self):
        call = calls(('run_shell', {'command': 'touch forbidden'}))['tool_calls'][0]
        with self.assertRaises(ToolArgumentError) as cm:
            parse_tool_arguments(call, TOOLS, {'_response_meta': {'finish_reason': 'length'}})
        self.assertEqual(cm.exception.code, 'OUTPUT_TRUNCATED')

    def test_all_tools_enforce_required_fields_and_declared_types(self):
        for tool in TOOLS:
            function = tool['function']
            for required in function['parameters'].get('required', []):
                with self.subTest(tool=function['name'], required=required):
                    with self.assertRaises(ToolArgumentError):
                        parse_tool_arguments(calls((function['name'], {}))['tool_calls'][0], TOOLS)
        for name, args in [('read_file', {'path': 5}), ('run_shell', {'command': 'test', 'timeout': True}),
                           ('write_files', {'files': [None]}), ('write_files', {'files': [{'content': 'x'}]}),
                           ('write_file', {'path': 'x', 'content': None}), ('read_file', []), ('write_files', {'files': []})]:
            with self.subTest(name=name, args=args), self.assertRaises(ToolArgumentError):
                parse_tool_arguments(calls((name, args))['tool_calls'][0], TOOLS)
        with self.assertRaises(ToolArgumentError):
            parse_tool_arguments(calls(('write_file', {'path': 'x', 'content': 'x'}))['tool_calls'][0], [t for t in TOOLS if t['function']['name'] == 'read_file'])

    def test_dynamic_browser_values_are_validated_before_any_action(self):
        valid = {'actions':[{'action':'fill_from_text','selector':'#expression','source_selector':'.solution code'},
                            {'action':'assert_value','selector':'#expression','value':''}]}
        self.assertEqual(parse_tool_arguments(calls(('browser_check', valid))['tool_calls'][0], TOOLS),valid)
        for bad in [{'action':'fill_from_text','selector':'#expression'},
                    {'action':'fill_from_text','source_selector':'.solution code'},
                    {'action':'assert_value','selector':'#expression','value':3}]:
            with self.assertRaises(ToolArgumentError):
                parse_tool_arguments(calls(('browser_check', {'actions':[{'action':'click','selector':'#answer'},bad]}))['tool_calls'][0], TOOLS)

    def test_bad_later_batch_item_never_writes_earlier_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for bad in [{'content': 'no path'}, {'path': 1, 'content': 'x'}, None]:
                with self.assertRaises(ValueError):
                    write_files(root, [{'path': 'first.py', 'content': 'good'}, bad])
                self.assertFalse((root / 'first.py').exists())

    def test_contextual_patch_and_stale_line_anchor_use_exact_unique_content(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'x').write_text('prefix\na\nb\nc\n')
            writer = lambda r, p, c: (r / p).write_text(c)
            apply_unified_patch(root, 'x', '@@\n a\n-b\n+B\n c\n', read_file, writer)
            self.assertEqual((root / 'x').read_text(), 'prefix\na\nB\nc\n')
            apply_unified_patch(root, 'x', '@@ -1,2 +1,2 @@\n a\n-B\n+bb\n', read_file, writer)
            self.assertEqual((root / 'x').read_text(), 'prefix\na\nbb\nc\n')

    def test_ambiguous_or_later_bad_patch_hunk_has_no_side_effect(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            original = 'a\nb\na\nb\n'
            (root / 'x').write_text(original)
            writer = lambda r, p, c: (r / p).write_text(c)
            for diff in ['@@\n a\n-b\n+B\n', '@@ -1,2 +1,2 @@\n a\n-b\n+B\n@@\n missing\n-x\n+y\n']:
                with self.assertRaises(ValueError):
                    apply_unified_patch(root, 'x', diff, read_file, writer)
                self.assertEqual((root / 'x').read_text(), original)


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture = patch('model_catalog.catalog', return_value=[{'id':'model','context':128000}])
        fixture.start()
        self.addCleanup(fixture.stop)

    async def test_oversized_hints_execute_once_and_report_actual_bounds(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / uuid.uuid4().hex
            root.mkdir(); (root / '.atoms').mkdir()
            (root / 'long.txt').write_text('a' * 16000)
            sequence = [calls(('write_files', {'files': [{'path': 'greet.py', 'content': 'print("hello")\n'},
                {'path': 'README.md', 'content': 'Run python greet.py; local CLI.'},
                {'path': 'other.txt', 'content': 'valid third file'}, {'path': 'extra.txt', 'content': 'valid fourth file'}]})),
                calls(('read_file', {'path': 'long.txt', 'limit': 20000})),
                calls(('run_shell', {'command': 'python greet.py', 'timeout': 300, 'requirement_ids': ['R1']})),
                calls(('update_task', {'id': 'T1', 'status': 'done', 'evidence_ids': ['V1']})), {'content': 'verified'}]
            observed = []
            class Gateway:
                def __init__(self, *a): pass
                async def chat(self, client, state, messages, tools=None, **kw):
                    if state == AgentState.REVIEW:
                        return {'content': '{"approved":true,"issues":[],"summary":"verified"}'}
                    return sequence.pop(0)
            from agent import run_shell
            with patch('agent.ensure_workspace', return_value=root), patch('agent.project_uid', return_value=os.getuid()), patch('agent.ModelGateway', Gateway), patch('agent.run_shell', wraps=run_shell) as shell:
                await run_agent(uuid.UUID(hex=root.name), 'Create greeting CLI', 'model',
                    lambda kind, label, detail, **kw: observed.append((kind, label, kw)), plan=json.dumps(cli_plan()))
                shell.assert_any_call(uuid.UUID(hex=root.name), 'python greet.py', SHELL_TIMEOUT_MAX)
            outputs = [x[2].get('tool_output', '') for x in observed if x[0] == 'tool']
            self.assertTrue(any('long.txt [0:12000/16000]' in x for x in outputs))
            self.assertTrue(any('"requested": 300, "effective": 180' in x for x in outputs))
            self.assertFalse(any('工具参数需修正' in x[1] for x in observed))
            self.assertTrue((root / 'extra.txt').exists())

    async def test_real_agent_rejects_bad_batch_then_recovers_without_partial_write(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / uuid.uuid4().hex
            root.mkdir(); (root / '.atoms').mkdir()
            bad = calls(('write_files', {'files': [{'path': 'forbidden.py', 'content': 'x'}, {'content': 'missing path'}]}))
            sequence = [bad, calls(('write_files', {'files': [{'path': 'greet.py', 'content': 'print("hello")\n'},
                                                             {'path': 'README.md', 'content': 'Run python greet.py; local CLI.'}]})),
                        calls(('run_shell', {'command': 'python greet.py', 'requirement_ids': ['R1']})),
                        calls(('update_task', {'id': 'T1', 'status': 'done', 'evidence_ids': ['V1']})), {'content': 'verified'}]
            observed = []
            class Gateway:
                def __init__(self, *a): pass
                async def chat(self, client, state, messages, tools=None, **kw):
                    if state == AgentState.REVIEW:
                        return {'content': '{"approved":true,"issues":[],"summary":"verified"}'}
                    if len(sequence) == 4:
                        assert not (root / 'forbidden.py').exists()
                        assert 'MISSING_ARGUMENT' in messages[-1]['content']
                    return sequence.pop(0)
            with patch('agent.ensure_workspace', return_value=root), patch('agent.project_uid', return_value=os.getuid()), patch('agent.ModelGateway', Gateway):
                await run_agent(uuid.UUID(hex=root.name), 'Create greeting CLI', 'model',
                                lambda kind, label, detail, **kw: observed.append((kind, label, kw)), plan=json.dumps(cli_plan()))
            self.assertFalse((root / 'forbidden.py').exists())
            self.assertTrue(json.loads((root / '.atoms/task-state.json').read_text())['completed'])
            self.assertTrue(any('工具参数需修正' in item[1] for item in observed))

    async def test_three_invalid_turns_skip_tool_and_complete_with_alternative(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / uuid.uuid4().hex
            root.mkdir(); (root / '.atoms').mkdir()
            sequence = [calls(('run_shell', {'command': 123})) for _ in range(3)] + [
                calls(('write_files', {'files':[{'path':'greet.py','content':'print("hello")\n'}, {'path':'README.md','content':'Run python greet.py'}, {'path':'.atoms-workspace.json','content':'{"test":"python greet.py"}'}]})),
                {'content':'Ready for actual project checks'},
                calls(('read_file', {'path':'greet.py'})),
                calls(('run_shell', {'command':'python greet.py','requirement_ids':['R1']})),
                calls(('update_task', {'id':'T1','status':'done','evidence_ids':['V1']})),
                {'content':'Greeting implemented and tested'}]
            observed=[]
            class Gateway:
                def __init__(self,*a): pass
                async def chat(self,client,state,messages,tools=None,**kw):
                    if len(sequence)==6:
                        assert 'run_shell' not in [tool['function']['name'] for tool in tools]
                    return sequence.pop(0)
            with patch('agent.ensure_workspace',return_value=root), patch('agent.project_uid',return_value=os.getuid()), patch('agent.ModelGateway',Gateway):
                await run_agent(uuid.UUID(hex=root.name),'Create greeting CLI','model',lambda kind,label,*a,**kw:observed.append(label),plan=json.dumps(cli_plan()))
            self.assertFalse(sequence)
            self.assertTrue((root/'greet.py').exists())
            self.assertIn('继续实现与交付',observed)

    def test_exact_browser_count_representation_is_normalized_without_weakening_assertion(self):
        for count in ['2', 2.0]:
            parsed=parse_tool_arguments(calls(('browser_check',{'actions':[{'action':'assert_count','selector':'.item','value':count}]}))['tool_calls'][0],TOOLS)
            self.assertEqual(parsed['actions'][0]['value'],2)
            self.assertIsInstance(parsed['actions'][0]['value'],int)
        for count in ['>=1',True,-1,1.5]:
            with self.assertRaises(ToolArgumentError):
                parse_tool_arguments(calls(('browser_check',{'actions':[{'action':'assert_count','value':count}]}))['tool_calls'][0],TOOLS)
