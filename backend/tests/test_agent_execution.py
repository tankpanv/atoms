import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from agent import run_agent, run_shell
from tool_limits import SHELL_COMMAND_MAX, SHELL_TIMEOUT_DEFAULT, SHELL_TIMEOUT_MAX
from coding_runtime import AgentState, AgentStateMachine


def cli_plan():
    return {"goal": "Working greeting CLI", "application_type": "cli",
            "architecture": {"frontend": {"stack": "none", "directory": "."},
                             "backend": {"required": False, "reason": "A local CLI", "stack": "none", "directory": "backend"}},
            "design": "A Python command with observable output",
            "commands": {"bootstrap": [], "build": [], "test": ["python greet.py"], "dev": ""},
            "requirements": [{"id": "R1", "description": "Print hello", "acceptance": ["Running python greet.py prints hello"], "verification": "command"}],
            "tasks": [{"id": "T1", "title": "Implement greeting", "requirement_ids": ["R1"], "depends_on": [], "files": ["greet.py"], "verification": "python greet.py"}]}


def calls(*items):
    return {"content": "", "tool_calls": [{"id": f"call{index}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
                                          for index, (name, arguments) in enumerate(items)]}


class AgentExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_shell_command_executes_once_and_rejects_overflow(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / uuid.uuid4().hex
            root.mkdir()
            prefix = "printf 'once\\n' >> executions.txt\n# "
            command = prefix + 'x' * (SHELL_COMMAND_MAX - len(prefix))
            with patch('agent.ensure_workspace', return_value=root), patch('agent.project_uid', return_value=os.getuid()):
                code, output = await run_shell(uuid.UUID(hex=root.name), command)
                self.assertEqual(code, 0, output)
                with self.assertRaises(ValueError):
                    await run_shell(uuid.UUID(hex=root.name), command + 'x')
            self.assertEqual((root / 'executions.txt').read_text(), 'once\n')

    async def test_shell_timeout_uses_extended_default_and_cap(self):
        with patch('agent.run_command', return_value=(0, '')) as execute:
            for requested, effective in [(None, SHELL_TIMEOUT_DEFAULT), (600, 600), (SHELL_TIMEOUT_MAX + 1, SHELL_TIMEOUT_MAX)]:
                options = {} if requested is None else {'timeout': requested}
                await run_shell(uuid.uuid4(), 'true', **options)
                self.assertEqual(execute.call_args.args[-1], effective)

    async def test_pipefail_preserves_failed_command_exit_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / uuid.uuid4().hex
            root.mkdir()
            with patch('agent.ensure_workspace', return_value=root), patch('agent.project_uid', return_value=os.getuid()):
                code, output = await run_shell(uuid.UUID(hex=root.name), "false | tail -1")
            self.assertNotEqual(code, 0)

    async def test_real_tools_receipts_and_functional_acceptance_complete_in_order(self):
        sequence = [calls(('write_file', {'path': 'greet.py', 'content': 'print("hello")\n'}),
                          ('write_file', {'path': 'README.md', 'content': 'Run python greet.py\n'})),
                    calls(('run_shell', {'command': 'python greet.py', 'requirement_ids': ['R1']})),
                    calls(('update_task', {'id': 'T1', 'status': 'done', 'evidence_ids': ['V1'], 'note': 'Real output verified'})),
                    {'content': 'Implemented and verified the greeting CLI.'}]

        class Gateway:
            def __init__(self, *args): pass
            async def chat(self, client, state, messages, tools=None, max_tokens=0):
                if state == AgentState.REVIEW:
                    raise AssertionError('Acceptance must not read every source file in a review phase')
                return sequence.pop(0)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / uuid.uuid4().hex
            root.mkdir(); (root/'.atoms').mkdir()
            machine = AgentStateMachine()
            def step(kind, label, detail, **metrics):
                if kind == 'state': machine.transition(AgentState(label))
            with patch('agent.ensure_workspace', return_value=root), patch('agent.project_uid', return_value=os.getuid()), patch('agent.ModelGateway', Gateway):
                result = await run_agent(uuid.UUID(hex=root.name), 'Create greeting CLI', 'test-model', step, plan=json.dumps(cli_plan()))
            self.assertEqual(machine.current, AgentState.COMPLETE)
            self.assertIn('验证通过', result['summary'])
            self.assertNotIn('尚未完成自动验证', result['summary'])
            state = json.loads((root/'.atoms/task-state.json').read_text())
            self.assertEqual(state['tasks'][0]['status'], 'done')
            self.assertIn('hello', state['evidence'][0]['output'])
            self.assertIn('greet.py', result['files'])

    async def test_document_only_changes_cannot_be_mistaken_for_implementation(self):
        sequence = [calls(('write_file', {'path': '.atoms/ARCHITECTURE.md', 'content': 'A proposed CLI'})), {'content': 'Done'}]
        class Gateway:
            def __init__(self, *args): pass
            async def chat(self, *args, **kwargs):
                if not sequence: raise RuntimeError('Provider unavailable; documentation cannot prove execution')
                return sequence.pop(0)
        states = []
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / uuid.uuid4().hex
            root.mkdir(); (root/'.atoms').mkdir()
            with patch('agent.ensure_workspace', return_value=root), patch('agent.project_uid', return_value=os.getuid()), patch('agent.ModelGateway', Gateway), patch.dict(os.environ, {'AGENT_MAX_ITERATIONS': '2'}):
                with self.assertRaises(RuntimeError):
                    await run_agent(uuid.UUID(hex=root.name), 'Create greeting CLI', 'test-model',
                                    lambda kind, label, detail, **metrics: states.append(label) if kind == 'state' else None,
                                    plan=json.dumps(cli_plan()))
            self.assertNotIn('COMPLETE', states)


if __name__ == '__main__':
    unittest.main()
