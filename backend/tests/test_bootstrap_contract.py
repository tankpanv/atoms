"""Exercise harness startup, not just the scaffold tool in isolation."""
import ast
import inspect
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import agent
from agent_harness import TaskLedger
from agent_session import AgentSession
from agent_delivery import DeliveryBudget
from coding_runtime import ModelGateway
from test_agent_harness import example_plan


class BootstrapContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # These executor tests do not call a provider. Keep the model context
        # fixture independent of the runtime-only /model_list mount, so the
        # same checks run before publishing an immutable worker image.
        catalog = patch('model_catalog.catalog',return_value=[{'id':'test','context':128000}])
        catalog.start()
        self.addCleanup(catalog.stop)

    async def test_empty_project_bootstrap_records_success_and_reaches_engineering(self):
        await self.exercise_bootstrap(0)

    async def test_failed_bootstrap_records_failure_without_fabricating_success(self):
        await self.exercise_bootstrap(1)

    async def exercise_bootstrap(self, exit_code):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/uuid.uuid4().hex; root.mkdir()
            plan = example_plan(); plan['architecture']['frontend']['stack'] = 'Vite React TypeScript'
            async def scaffold(*args):
                if exit_code == 0:
                    (root/'frontend/src').mkdir(parents=True)
                    (root/'frontend/package.json').write_text('{"name":"actual-template"}')
                    (root/'frontend/src/main.tsx').write_text('// actual template entry\n')
                return exit_code, 'CLI successful' if exit_code == 0 else 'CLI install failed'
            reached = []
            class Gateway:
                def __init__(self,*args): pass
                async def chat(self, client, state, messages, *args, **kwargs):
                    reached.append(messages)
                    raise RuntimeError('Reached engineering after bootstrap')
            with patch('agent.ensure_workspace',return_value=root), patch('agent.project_uid',return_value=os.getuid()), patch('project_templates.select_template',return_value=None), patch('agent.scaffold_project',AsyncMock(side_effect=scaffold)) as initialize, patch('agent.ModelGateway',Gateway):
                with self.assertRaisesRegex(RuntimeError,'Reached engineering after bootstrap'):
                    await agent.run_agent(uuid.UUID(hex=root.name),'Calendar','test',lambda *args,**kwargs:None,plan=json.dumps(plan))
                initialize.assert_awaited_once()
            evidence = json.loads((root/'.atoms/task-state.json').read_text())['evidence']
            self.assertEqual(len(evidence),1)
            self.assertEqual(evidence[0]['kind'],'scaffold_project')
            self.assertEqual(evidence[0]['exit_code'],exit_code)
            self.assertEqual(evidence[0]['id'],'V1')
            self.assertEqual(len(reached),1)
            if exit_code == 0:
                self.assertIn('actual-template',json.dumps(reached[0]))
            # Initialization evidence survives continuation; failed initialization
            # does not satisfy the real-bootstrap acceptance gate.
            restored = TaskLedger(root,plan,'Calendar')
            issues = restored.completion_issues(agent.snapshot_files(root))
            self.assertEqual(any('缺少真实框架 CLI' in issue for issue in issues),exit_code != 0)

    async def test_existing_frontend_is_preserved_and_not_scaffolded_again(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/uuid.uuid4().hex; root.mkdir()
            (root/'frontend/src').mkdir(parents=True)
            (root/'frontend/package.json').write_text('{}')
            (root/'frontend/src/App.tsx').write_text('// original application\n')
            plan=example_plan(); plan['architecture']['frontend']['stack']='Vite React TypeScript'
            class Gateway:
                def __init__(self,*args): pass
                async def chat(self,*args,**kwargs): raise RuntimeError('Reached existing project')
            with patch('agent.ensure_workspace',return_value=root), patch('agent.project_uid',return_value=os.getuid()), patch('agent.scaffold_project',AsyncMock()) as initialize, patch('agent.ModelGateway',Gateway):
                with self.assertRaisesRegex(RuntimeError,'Reached existing project'):
                    await agent.run_agent(uuid.UUID(hex=root.name),'Calendar','test',lambda *args,**kwargs:None,plan=json.dumps(plan))
                initialize.assert_not_awaited()
            self.assertEqual((root/'frontend/src/App.tsx').read_text(),'// original application\n')

    def test_internal_method_names_and_signatures_match_real_implementations(self):
        classes={'ledger':TaskLedger,'session':AgentSession,'budget':DeliveryBudget,'gateway':ModelGateway}
        tree=ast.parse(Path(agent.__file__).read_text())
        checked=0
        for node in ast.walk(tree):
            if not isinstance(node,ast.Call) or not isinstance(node.func,ast.Attribute) or not isinstance(node.func.value,ast.Name):
                continue
            owner=classes.get(node.func.value.id)
            if owner is None:
                continue
            name=node.func.attr
            method=getattr(owner,name,None)
            self.assertTrue(callable(method),f'{owner.__name__}.{name} at agent.py:{node.lineno} does not exist')
            if any(isinstance(arg,ast.Starred) for arg in node.args) or any(k.arg is None for k in node.keywords):
                continue
            try:
                inspect.signature(method).bind(None,*[None for _ in node.args],**{k.arg:None for k in node.keywords})
            except TypeError as exc:
                self.fail(f'{owner.__name__}.{name} at agent.py:{node.lineno}: {exc}')
            checked+=1
        self.assertGreater(checked,20)
