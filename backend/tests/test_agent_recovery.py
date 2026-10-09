"""Availability recovery must retain budgets and prove the delivered result."""
import json
import os
import tempfile
import unittest
import uuid
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from agent import run_agent, run_build, verify_delivery
from billing_context import active_job
from agent_session import AgentSession
from coding_runtime import configured_tests, ModelTemporaryError
from test_agent_execution import cli_plan


class UnavailableGateway:
    def __init__(self, *args): pass
    async def chat(self, *args, **kwargs):
        raise ModelTemporaryError('Temporary provider outage')


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        catalog = patch('model_catalog.catalog', return_value=[{'id':'test-model','context':128000}])
        catalog.start()
        self.addCleanup(catalog.stop)

    def test_runner_precedence_and_unittest_detection(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/'tests').mkdir()
            (root/'tests/test_app.py').write_text('import unittest\n')
            self.assertEqual(configured_tests(root),['python -m unittest discover -s tests'])
            self.assertEqual(configured_tests(root,['python check.py']),['python check.py'])
            (root/'.atoms-workspace.json').write_text(json.dumps({'test':'python explicit.py'}))
            self.assertEqual(configured_tests(root,['python check.py']),['python explicit.py'])

    async def test_planned_build_and_cli_verification_use_actual_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/uuid.uuid4().hex;root.mkdir();(root/'tests').mkdir()
            (root/'greet.py').write_text('print("hello")\n')
            (root/'tests/test_app.py').write_text('import unittest\n')
            with patch('agent.ensure_workspace',return_value=root),patch('agent.project_uid',return_value=os.getuid()):
                code,output=await run_build(uuid.UUID(hex=root.name),['python greet.py'])
                check,report=await verify_delivery(uuid.UUID(hex=root.name),root,cli_plan())
            self.assertEqual(code,0);self.assertIn('hello',output)
            self.assertEqual(check,0);self.assertIn('python greet.py',report)
            self.assertNotIn('pytest',report)

    async def test_outage_checks_existing_result_and_reuses_same_job_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/uuid.uuid4().hex;root.mkdir();(root/'.atoms').mkdir()
            (root/'greet.py').write_text('from pathlib import Path\nPath("proof.txt").write_text("executed")\nprint("hello")\n')
            token=active_job.set(str(uuid.uuid4()))
            try:
                with patch('agent.ensure_workspace',return_value=root),patch('agent.project_uid',return_value=os.getuid()),patch('agent.ModelGateway',UnavailableGateway):
                    first=await run_agent(uuid.UUID(hex=root.name),'Create greeting CLI','test-model',lambda *args,**kw:None,
                        plan=json.dumps(cli_plan()),initial_tokens=Decimal('50'),initial_calls=Decimal('2'),availability_fallback=False)
                    self.assertEqual(first['delivery'],'demo')
                    self.assertTrue((root/'DELIVERY_TODO.md').exists())
                    before=AgentSession(root).state['delivery_budget']
                    result=await run_agent(uuid.UUID(hex=root.name),'Create greeting CLI','test-model',lambda *args,**kw:None,
                        plan=json.dumps(cli_plan()))
                after=AgentSession(root).state['delivery_budget']
            finally: active_job.reset(token)
            self.assertEqual((root/'proof.txt').read_text(),'executed')
            self.assertEqual(result['delivery'],'demo')
            self.assertEqual(after['task_tokens'],50)
            self.assertEqual(after['task_calls'],2)
            self.assertGreaterEqual(after['task_iterations'],before['task_iterations'])
            self.assertEqual(after['deadline_at'],before['deadline_at'])
            self.assertFalse(json.loads((root/'.atoms/task-state.json').read_text()).get('completed'))

    async def test_outage_cannot_claim_a_broken_result_is_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/uuid.uuid4().hex;root.mkdir();(root/'.atoms').mkdir()
            (root/'greet.py').write_text('raise RuntimeError("broken")\n')
            states=[]
            with patch('agent.ensure_workspace',return_value=root),patch('agent.project_uid',return_value=os.getuid()),patch('agent.ModelGateway',UnavailableGateway):
                with self.assertRaises(ModelTemporaryError):
                    await run_agent(uuid.UUID(hex=root.name),'Create greeting CLI','test-model',
                        lambda kind,label,detail,**kw:states.append(label) if kind=='state' else None,plan=json.dumps(cli_plan()))
            self.assertNotIn('PREVIEW_READY',states)
            self.assertNotIn('COMPLETE',states)
