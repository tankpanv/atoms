import copy
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agent_harness import validate_plan
from coding_runtime import configured_tests, has_build_command
from execution_contract import validate_command, resolve_default_plan, conformance_issues, workspace_commands
from execution_guard import ExecutionGuard
from runtime import detected_command
from test_project_templates import default_plan
from test_agent_execution import cli_plan, calls
from coding_runtime import AgentState


class ExecutableContractTests(unittest.TestCase):
    def test_prose_is_rejected_while_real_shell_and_unicode_arguments_work(self):
        for bad in ['按现有项目约定执行测试；若是新项目使用 npm test', '沿用现有命令', 'Use npm test', 'if needed run pytest']:
            with self.assertRaises(ValueError): validate_command(bad)
        for command in ['npm test', "python '生成报告.py'", "echo '使用中文说明'", 'CI=1 npm test', 'if test -f x; then python x; fi', 'false | tail -1']:
            self.assertEqual(validate_command(command), command)
        plan = default_plan()
        plan['commands']['test'] = ['按现有项目约定执行测试']
        with self.assertRaises(ValueError): validate_plan(plan)
        plan = default_plan(); plan['architecture']['frontend']['directory'] = '沿用现有目录；新项目使用 frontend'
        with self.assertRaises(ValueError): validate_plan(plan)
        plan = default_plan(); plan['tasks'][0]['files'] = ['现有项目的相关页面']
        with self.assertRaises(ValueError): validate_plan(plan)

    def test_new_default_is_deterministic_in_both_tiers_and_preserves_requested_stack(self):
        for tier in ('normal', 'deep', 'advanced'):
            plan = default_plan();plan['build_tier'] = tier
            plan['architecture']['frontend']['directory'] = '沿用现有；新项目 frontend'
            plan['commands']['test'] = ['按项目约定执行测试']
            fixed = resolve_default_plan(plan, '制作完整健康数据应用', [], new_project=True)
            self.assertEqual(fixed['architecture']['frontend']['directory'], 'frontend')
            self.assertEqual(fixed['commands']['test'], [])
            self.assertEqual(fixed['commands']['dev'], 'npm run dev')
            self.assertIn('shadcn/ui', fixed['architecture']['frontend']['stack'])
            self.assertEqual(resolve_default_plan(plan, '使用 Vue', [], new_project=True), plan)
            self.assertEqual(resolve_default_plan(plan, '扩展功能', [], new_project=False), plan)

    def test_nested_runner_repair_overrides_frozen_plan_consistently(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'.atoms-workspace.json').write_text(json.dumps({'commands': {'build':'npm run build', 'dev':'npm run dev -- --base $BASE_PATH', 'test':'npm test'}}))
            # This exact shape was repeatedly written by the real failing agent.
            self.assertEqual(configured_tests(root, ['按现有项目约定执行测试']), ['npm test'])
            self.assertTrue(has_build_command(root))
            with patch('runtime.ensure_workspace', return_value=root):
                self.assertEqual(detected_command(uuid.uuid4()), ('npm run dev -- --base $BASE_PATH', True))
            (root/'.atoms-workspace.json').write_text(json.dumps({'test':'python -m unittest', 'commands': {'test':'npm test'}}))
            self.assertEqual(configured_tests(root), ['python -m unittest'])

    def test_actual_root_modules_and_template_drift_are_detected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); plan = default_plan()
            self.assertTrue(conformance_issues(root, plan, {'src/main.tsx':'app'}))
            plan['architecture']['frontend']['directory'] = '.'
            self.assertEqual(conformance_issues(root, plan, {'src/main.tsx':'app'}), [])
            self.assertEqual(conformance_issues(root, plan, {'src/main.tsx':'entry', 'src/domain.ts':'logic'}), [])

    def test_source_directory_and_project_directory_both_match_without_moving_business(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = default_plan()
            for directory, source in [('src', 'src/App.tsx'), ('.', 'src/App.tsx'),
                                      ('./src', 'src/App.tsx'), ('frontend', 'frontend/src/App.tsx'),
                                      ('frontend/src', 'frontend/src/App.tsx'), ('web', 'web/src/App.tsx')]:
                with self.subTest(directory=directory):
                    plan['architecture']['frontend']['directory'] = directory
                    self.assertEqual(conformance_issues(root, plan, {source: 'existing app'}), [])
            plan['architecture']['frontend']['directory'] = 'frontend'
            issues = conformance_issues(root, plan, {'src/App.tsx': 'existing app'})
            self.assertIn('src/App.tsx', issues[0])


class GuardTests(unittest.TestCase):
    def test_browser_failure_keeps_actual_error_and_controls_before_clipping(self):
        guard=ExecutionGuard({});args={'path':'/report'}
        output=json.dumps({'error':'Wrong selector: expected link but actual control is a button','controls':[{'tag':'button','selector':'#edit','text':'编辑'}], 'text':'large page '*4000})
        guard.record('browser','browser_check',args,'current',1,output)
        diagnostic=guard.diagnostic('current')
        self.assertIn('Wrong selector',diagnostic)
        self.assertIn('#edit',diagnostic)
        self.assertLess(len(guard.entries['browser']['output']),12000)

    def test_failures_span_turns_restart_labels_and_timeouts_but_changes_allow_retry(self):
        state={}; guard=ExecutionGuard(state)
        args={'command':'npm test','timeout':10,'requirement_ids':['R1']}
        key=guard.key('run_shell', args, 'source-a')
        guard.record(key,'run_shell',args,'source-a',127,'missing command')
        self.assertIsNone(guard.prior(key))
        guard.record(key,'run_shell',args,'source-a',127,'missing command')
        restarted=ExecutionGuard(json.loads(json.dumps(state)))
        repeated=restarted.key('run_shell',{'command':'npm test','timeout':180,'requirement_ids':['R2']},'source-a')
        self.assertIsNotNone(restarted.prior(repeated))
        self.assertIsNone(restarted.prior(restarted.key('run_shell',args,'source-b')))
        guard.record(key,'run_shell',args,'source-a',0,'actual pass')
        self.assertIsNone(guard.prior(key))  # arbitrary successful shell may mutate data
        self.assertEqual(guard.prior(key,reusable=True)['code'],0)

    def test_receipts_and_notes_cannot_disguise_no_progress(self):
        guard=ExecutionGuard({}); tasks=[{'id':'T1','status':'in_progress','note':'one'}]
        self.assertFalse(guard.observe('same-source',tasks))
        for i in range(1,4):
            tasks[0]['note']=str(i)
            self.assertEqual(guard.observe('same-source',tasks),i==3)
        self.assertFalse(guard.observe('changed-source',tasks))
        tasks[0]['status']='done'
        self.assertFalse(guard.observe('changed-source',tasks))

    def test_recovery_excludes_errors_from_repaired_source_versions(self):
        guard=ExecutionGuard({})
        args={'command':'npm test'}
        old=guard.key('run_shell',args,'old-source')
        guard.record(old,'run_shell',args,'old-source',1,'OLD_SYNTAX_ERROR')
        fixed=guard.key('run_shell',args,'repaired-source')
        guard.record(fixed,'run_shell',args,'repaired-source',0,'actual tests passed')
        self.assertEqual(guard.failures('repaired-source'),[])
        self.assertNotIn('OLD_SYNTAX_ERROR',guard.diagnostic('repaired-source'))
        self.assertIn('OLD_SYNTAX_ERROR',guard.diagnostic('old-source'))


class RealExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_schema_floor_does_not_cause_repeated_model_summaries(self):
        import agent
        sequence = [calls(('write_files', {'files': [
            {'path':'greet.py','content':'print("hello")'},
            {'path':'README.md','content':'Run python greet.py'}]}))]
        sequence += [calls(('read_file', {'path':'greet.py'})) for _ in range(12)]
        sequence += [calls(('run_shell', {'command':'python greet.py','requirement_ids':['R1']})),
                     calls(('update_task', {'id':'T1','status':'done','evidence_ids':['V1']})),
                     {'content':'Implemented and verified greeting'}]
        summaries=[]
        class Gateway:
            def __init__(self,*args): pass
            async def chat(self,client,state,messages,tools=None,**kwargs):
                if state == AgentState.COMPACT:
                    summaries.append(messages)
                    return {'content':'The original greeting goal remains; greet.py is implemented, next verify python greet.py and complete T1.'}
                if state == AgentState.PLAN:
                    return {'content':json.dumps({'layer':'stagnation','root_cause':'Repeated reads do not advance the existing CLI','next_actions':['Verify python greet.py and update T1']})}
                if not sequence: raise AssertionError('Execution did not finish')
                return sequence.pop(0)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/uuid.uuid4().hex;root.mkdir()
            with patch('agent.ensure_workspace',return_value=root),patch('agent.project_uid',return_value=os.getuid()),patch('agent.ModelGateway',Gateway),patch('model_catalog.catalog',return_value=[{'id':'test','context':128000}]),patch.dict('os.environ',{'AGENT_CONTEXT_TOKENS':'4096'}):
                result=await agent.run_agent(uuid.UUID(hex=root.name),'Create greeting CLI','test',lambda *args,**kwargs:None,plan=json.dumps(cli_plan()))
            self.assertLessEqual(len(summaries),1)
            self.assertTrue(json.loads((root/'.atoms/task-state.json').read_text())['completed'])

    async def test_repeated_real_failure_is_diagnosed_and_alternative_completes(self):
        import agent
        sequence = [calls(('write_files', {'files':[{'path':'greet.py','content':'print("hello")'},
                            {'path':'README.md','content':'Run python greet.py'}]})),
                    calls(('run_shell', {'command':'missing_runner'})),
                    calls(('run_shell', {'command':'missing_runner'})),
                    calls(('run_shell', {'command':'missing_runner'})),
                    calls(('run_shell', {'command':'python greet.py','requirement_ids':['R1']})),
                    calls(('update_task', {'id':'T1','status':'done','evidence_ids':['V4']})),
                    {'content':'Implemented and verified greeting'}]
        diagnoses=[]
        class Gateway:
            def __init__(self,*args):pass
            async def chat(self,client,state,messages,tools=None,**kwargs):
                if state == AgentState.PLAN:
                    diagnoses.append(messages)
                    return {'content':json.dumps({'layer':'command','root_cause':'missing_runner is not installed; this is a runner error, not broken Python business code','next_actions':['Run python greet.py and associate R1 evidence']})}
                if not sequence:raise AssertionError(json.dumps(messages[-4:], ensure_ascii=False))
                return sequence.pop(0)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/uuid.uuid4().hex;root.mkdir()
            real_shell=agent.run_shell;executed=[]
            async def capture(pid,command,timeout=180):
                executed.append(command);return await real_shell(pid,command,timeout)
            with patch('agent.ensure_workspace',return_value=root),patch('agent.project_uid',return_value=os.getuid()),patch('agent.ModelGateway',Gateway),patch('agent.run_shell',side_effect=capture),patch('model_catalog.catalog',return_value=[{'id':'test','context':128000}]),patch('runtime.runtime_status',return_value={'running':True,'output':'RuntimeError: REAL_BACKEND_ROOT_CAUSE'}):
                result=await agent.run_agent(uuid.UUID(hex=root.name),'Create greeting CLI','test',lambda *args,**kwargs:None,plan=json.dumps(cli_plan()))
            self.assertEqual(executed.count('missing_runner'),2)
            self.assertGreaterEqual(len(diagnoses),1)
            self.assertIn('command not found',str(diagnoses[0]))
            self.assertIn('actual_failed_operations',str(diagnoses[0]))
            self.assertTrue(json.loads((root/'.atoms/task-state.json').read_text())['completed'])

    async def test_timeout_keeps_output_for_root_cause_diagnosis(self):
        import agent
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/uuid.uuid4().hex;root.mkdir()
            with patch('agent.ensure_workspace',return_value=root),patch('agent.project_uid',return_value=os.getuid()):
                code,output=await agent.run_shell(uuid.UUID(hex=root.name), "python -u -c 'import time; print(\"WAITING_FOR_INPUT\"); time.sleep(10)'", 1)
            self.assertEqual(code,124)
            self.assertIn('WAITING_FOR_INPUT',output)
            self.assertIn('进程组',output)

    async def test_nested_build_executes_real_command(self):
        import agent
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/uuid.uuid4().hex;root.mkdir()
            (root/'.atoms-workspace.json').write_text(json.dumps({'commands':{'build': "python -c 'print(12345)'"}}))
            with patch('agent.ensure_workspace',return_value=root),patch('agent.project_uid',return_value=os.getuid()):
                code,output=await agent.run_build(uuid.UUID(hex=root.name))
            self.assertEqual(code,0);self.assertIn('12345',output)
