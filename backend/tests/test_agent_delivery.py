import json
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch, AsyncMock

from agent import AgentStopped, run_agent, verify_delivery
from agent_delivery import DeliveryBudget, DeliveryLimitReached, ExecutionPacing, complexity
from coding_runtime import AgentState, AgentStateMachine
from test_agent_execution import cli_plan, calls


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture=patch('model_catalog.catalog',return_value=[{'id':'test','context':128000}])
        fixture.start()
        self.addCleanup(fixture.stop)

    def test_soft_pacing_detects_stall_without_changing_scope_or_budget(self):
        ledger=type('Ledger',(),{'tasks':[{'id':'T1','title':'Complete original feature','status':'in_progress'}],'evidence':[]})()
        budget=DeliveryBudget(1000000,120,time.monotonic()+3600,task_iterations=16,task_calls=20)
        pacing=ExecutionPacing(16)
        for _ in range(3): note,_=pacing.guide(ledger,[],budget)
        self.assertIn('定位一个具体阻塞',note)
        self.assertIn('保留全部需求',note)
        self.assertFalse(budget.due())
        self.assertEqual(budget.phase,'implementation')
        pacing.guide(ledger,[{'tool':'write_file','result':'Written actual source'}],budget)
        self.assertEqual(pacing.stagnant,0)

    async def test_planning_calls_do_not_turn_simple_task_soft_target_into_demo_cutoff(self):
        sequence=[calls(('write_files',{'files':[{'path':'greet.py','content':'print("hello")\n'},
                    {'path':'.atoms-workspace.json','content':'{"test":"python greet.py"}'},
                    {'path':'README.md','content':'Run python greet.py'}]})),
                  calls(('run_shell',{'command':'python greet.py','requirement_ids':['R1']})),
                  calls(('update_task',{'id':'T1','status':'done','evidence_ids':['V1'],'note':'Actual output verified'})),
                  {'content':'Greeting fully implemented and verified'}]
        class Gateway:
            def __init__(self,*args): pass
            async def chat(self,client,state,*args,**kwargs):
                if state==AgentState.REVIEW: return {'content':'{"approved":true,"issues":[],"summary":"Verified"}'}
                return sequence.pop(0)
        states=[]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/uuid.uuid4().hex; root.mkdir()
            with patch('agent.ensure_workspace',return_value=root),patch('agent.project_uid',return_value=os.getuid()),patch('agent.ModelGateway',Gateway),patch.dict(os.environ,{'AGENT_MAX_ITERATIONS':'120'}):
                result=await run_agent(uuid.uuid4(),'Create greeting','test',lambda kind,label,*args,**kwargs:states.append(label) if kind=='state' else None,
                    initial_calls=20,plan=json.dumps(cli_plan()))
            self.assertIn('COMPLETE',states)
            self.assertNotIn('STABILIZE',states)
            self.assertNotEqual(result.get('delivery'),'demo')
            from agent_session import AgentSession
            self.assertEqual(AgentSession(root).state['delivery_budget']['iteration_limit'],120)

    async def test_existing_correct_implementation_can_be_verified_without_forced_rewrites(self):
        sequence=[calls(('run_shell',{'command':'python greet.py','requirement_ids':['R1']})),
                  calls(('update_task',{'id':'T1','status':'done','evidence_ids':['V1'],'note':'Existing code verified'})),
                  {'content':'Verified existing requested behavior'}]
        class Gateway:
            def __init__(self,*args): pass
            async def chat(self,client,state,*args,**kwargs):
                if state==AgentState.REVIEW: return {'content':'{"approved":true,"issues":[],"summary":"Verified"}'}
                return sequence.pop(0)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'greet.py').write_text('print("hello")\n')
            (root/'README.md').write_text('Run python greet.py')
            with patch('agent.ensure_workspace',return_value=root),patch('agent.project_uid',return_value=os.getuid()),patch('agent.ModelGateway',Gateway):
                result=await run_agent(uuid.uuid4(),'Verify greeting','test',lambda *args,**kwargs:None,plan=json.dumps(cli_plan()))
            self.assertLessEqual(len(sequence), 1)
            self.assertEqual((root/'greet.py').read_text(),'print("hello")\n')
            self.assertNotEqual(result.get('delivery'),'demo')

    async def test_scene_protocol_failure_requires_core_check_and_preserves_business_code(self):
        from delivery_checks import DeliverySceneError
        from agent_session import AgentSession
        sequence=[calls(('run_shell',{'command':'python greet.py','requirement_ids':['R1']})),
                  calls(('update_task',{'id':'T1','status':'done','evidence_ids':['V1'],'note':'Actual command verified'})),
                  {'content':'Ready to verify the application'},
                  calls(('write_file', {'path':'.atoms-workspace.json','content':json.dumps({'build':'echo actual-build-check','test':'python greet.py','demo':{'actions':[{'action':'assert_text','selector':'#result','value':'hello'}]}})})),
                  calls(('browser_check',{'actions':[{'action':'assert_text','selector':'#result','value':'hello'}],'requirement_ids':['R1']})),
                  calls(('run_shell',{'command':'python greet.py','requirement_ids':['R1']})),
                  calls(('update_task',{'id':'T1','status':'done','evidence_ids':['V3'],'note':'Fresh core workflow and command verified'})),
                  {'content':'Core interaction contract supplied'}]
        class Gateway:
            def __init__(self,*args): pass
            async def chat(self,client,state,*args,**kwargs):
                if state==AgentState.TEST: return {'content':'not valid scene JSON'}
                if state==AgentState.PLAN: return {'content':json.dumps({'root_cause':'当前场景无法解析，须基于真实控件校正','hypotheses':[{'cause':'场景合同错误','probe':'检查真实控件','supports':'控件存在','refutes':'控件不存在'},{'cause':'业务流程错误','probe':'实际点击提交并读回','supports':'业务错误','refutes':'操作成功'}],'next_actions':['修复场景并执行实际核心链路']})}
                if state==AgentState.REVIEW: return {'content':'{"approved":true,"issues":[],"summary":"Verified"}'}
                if not sequence: raise AssertionError(json.dumps(args[0][-4:],ensure_ascii=False)[-9000:])
                return sequence.pop(0)
        states=[]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/uuid.uuid4().hex;root.mkdir()
            (root/'greet.py').write_text('print("hello")\n');(root/'README.md').write_text('Existing implementation')
            (root/'.atoms-workspace.json').write_text('{"build":"echo actual-build-check","test":"python greet.py"}')
            (root/'package.json').write_text('{}')
            plan=cli_plan();plan['application_type']='web'
            with patch('agent.ensure_workspace',return_value=root),patch('agent.project_uid',return_value=os.getuid()),patch('agent.ModelGateway',Gateway),patch('runtime.start_runtime',AsyncMock(return_value=object())),patch('agent_checks.browser_check',AsyncMock(return_value=(0,'{"text":"Actual product","rendered_elements":4}'))):
                result=await run_agent(uuid.UUID(hex=root.name),'Verify existing product','test',lambda kind,label,*args,**kwargs:states.append(label) if kind=='state' else None,plan=json.dumps(plan))
            self.assertNotEqual(result.get('verification'),'runtime_and_rendering')
            self.assertLessEqual(len(sequence), 1)
            self.assertIn('COMPLETE',states)
            saved=AgentSession(root).state
            self.assertTrue(saved['demo']['core_interactions_verified'])
            self.assertIn('scene_diagnostic',saved)
            self.assertEqual((root/'greet.py').read_text(),'print("hello")\n')

    async def test_scene_gap_can_deliver_real_startup_demo_without_fake_full_acceptance(self):
        from delivery_checks import DeliverySceneError
        from agent_session import AgentSession
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/uuid.uuid4().hex; root.mkdir()
            (root/'greet.py').write_text('print("hello")\n')
            (root/'.atoms-workspace.json').write_text('{"build":"echo build","test":"python greet.py"}')
            plan=cli_plan();plan['application_type']='web'
            class Gateway:
                def __init__(self,*a):pass
                async def chat(self,*a,**kw):return {'content':'Run actual verification'}
            preview_attempts=[]
            async def verify(*a,**kw):
                if kw.get('preview_only'):
                    preview_attempts.append(True)
                    if len(preview_attempts)==1:return 1,'Actual JavaScript startup crashed'
                    return 0,'Actual live product rendered without runtime errors'
                raise DeliverySceneError('Invalid scene JSON')
            # Enter the bounded delivery phase to exercise generic final recovery.
            with patch('agent.ensure_workspace',return_value=root),patch('agent.project_uid',return_value=os.getuid()),patch('agent.ModelGateway',Gateway),patch('agent.verify_delivery',side_effect=verify),patch.dict(os.environ,{'AGENT_MAX_ITERATIONS':'6'}):
                result = await run_agent(uuid.UUID(hex=root.name),'Verify product','test',lambda *a,**kw:None,initial_calls=5,plan=json.dumps(plan))
            self.assertEqual(result['delivery'], 'demo')
            self.assertEqual(len(preview_attempts), 2)
            self.assertIn('TODO', result['summary'])
            state=AgentSession(root).state
            self.assertTrue(state['demo']['ready'])
            self.assertFalse(state['demo']['core_interactions_verified'])
            self.assertTrue((root/'DELIVERY_TODO.md').is_file())
            self.assertEqual((root/'greet.py').read_text(), 'print("hello")\n')
            self.assertFalse(json.loads((root/'.atoms/task-state.json').read_text())['completed'])

    def test_scope_and_separate_accounting(self):
        self.assertEqual(complexity(cli_plan()), ('simple', 16))
        small = cli_plan(); small['requirements'] *= 4
        self.assertEqual(complexity(small)[0], 'simple')
        plan = cli_plan(); plan['architecture']['backend']['required'] = True
        self.assertEqual(complexity(plan)[0], 'medium')
        budget = DeliveryBudget(10000, 5, time.monotonic() + 3600, task_tokens=8500)
        self.assertTrue(budget.due())
        budget.phase = 'stabilization'
        for _ in range(20): budget.record(50000)
        self.assertEqual(budget.task_tokens, 8500)
        self.assertEqual(budget.repair_tokens, 1000000)
        self.assertEqual(budget.snapshot()['total_tokens'], 1008500)
        self.assertFalse(budget.due())
        self.assertEqual(budget.repair_exhaustion(), 'token')
        fresh = DeliveryBudget(10000, 120, time.monotonic() + 3600)
        self.assertTrue(fresh.due(reserve_tokens=12000))

    def test_delivery_has_separate_token_call_and_iteration_stops(self):
        budget = DeliveryBudget(10000, 5, time.monotonic()+3600, phase='stabilization',
                                repair_token_limit=50000, repair_iteration_limit=2)
        budget.repair_iterations = 2
        self.assertEqual(budget.repair_exhaustion(), 'iterations')
        self.assertIsNone(budget.repair_exhaustion(include_iterations=False))
        budget.repair_iterations = 0
        budget.record(1000); budget.record(1000)
        self.assertEqual(budget.repair_exhaustion(), 'model_calls')
        self.assertEqual(budget.task_tokens, 0)
        self.assertEqual(budget.snapshot()['repair_token_limit'], 50000)

    def test_delivery_defaults_are_half_configured_limits_not_adaptive_iterations(self):
        budget = DeliveryBudget(15000000,16,time.monotonic()+3600,configured_iteration_limit=120)
        self.assertEqual(budget.repair_token_limit,7500000)
        self.assertEqual(budget.repair_iteration_limit,60)
        smaller = DeliveryBudget(999,3,time.monotonic()+3600,configured_iteration_limit=9)
        self.assertEqual(smaller.repair_token_limit,499)
        self.assertEqual(smaller.repair_iteration_limit,4)
        minimum = DeliveryBudget(1,1,time.monotonic()+3600)
        self.assertEqual(minimum.repair_token_limit,1)
        self.assertEqual(minimum.repair_iteration_limit,1)

    async def test_delivery_budget_stops_repeated_reads_and_preserves_checkpoint(self):
        for limit, tokens, expected in [('1',1000,'model_calls'),('20',500000,'token')]:
            count = []
            class Gateway:
                def __init__(self, model, usage, *args): self.usage = usage
                async def chat(self, client, state, messages, *args, **kwargs):
                    count.append(state)
                    self.usage(state.value, {'total_tokens':tokens,'model':'test','duration_ms':1})
                    return calls(('read_file',{'path':'greet.py'}))
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)/uuid.uuid4().hex; root.mkdir()
                (root/'greet.py').write_text('raise ValueError("not ready")\n')
                with patch('agent.ensure_workspace',return_value=root), patch('agent.project_uid',return_value=os.getuid()), patch('agent.ModelGateway',Gateway), patch.dict(os.environ,{'AGENT_MAX_ITERATIONS':str(int(limit)*2),'AGENT_MAX_TOKENS':'1000000'}):
                    with self.assertRaises(DeliveryLimitReached):
                        await run_agent(uuid.UUID(hex=root.name),'Greeting','test',lambda *args,**kwargs:None,
                                        initial_tokens=20000000,plan=json.dumps(cli_plan()))
                self.assertEqual(len(count),1)
                from agent_session import AgentSession
                state = AgentSession(root).state
                self.assertEqual(state['delivery_limit_reached'],expected)
                self.assertFalse(state.get('demo',{}).get('ready'))
                self.assertFalse(state.get('completed'))
                self.assertEqual(state['delivery_budget']['repair_calls'],1)
                self.assertIn('not ready',(root/'greet.py').read_text())

    async def test_delivery_preflight_refuses_model_request_without_context_allowance(self):
        count=[]
        class Gateway:
            def __init__(self,*args): pass
            async def chat(self,*args,**kwargs): count.append(1); raise AssertionError('request must not run')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/'greet.py').write_text('print("hello")')
            with patch('agent.ensure_workspace',return_value=root), patch('agent.project_uid',return_value=os.getuid()), patch('agent.ModelGateway',Gateway), patch('agent.estimate_tokens',return_value=10000), patch.dict(os.environ,{'AGENT_MAX_TOKENS':'20000','AGENT_CONTEXT_TOKENS':'1000000'}):
                with self.assertRaises(DeliveryLimitReached):
                    await run_agent(uuid.uuid4(),'Greeting','test',lambda *args,**kwargs:None,
                                    initial_tokens=20000000,plan=json.dumps(cli_plan()))
            self.assertEqual(count,[])

    async def test_repairs_beyond_primary_iteration_and_token_limits_keep_pending_contract(self):
        sequence = [calls(('write_files', {'files': [
            {'path': 'greet.py', 'content': 'raise ValueError("broken demo")\n'},
            {'path': '.atoms-workspace.json', 'content': json.dumps({'test': 'python greet.py'})}]})),
            calls(('replace_in_file', {'path': 'greet.py', 'old': 'raise ValueError("broken demo")', 'new': 'print("hello")'}))]
        machine = AgentStateMachine(); states = []
        class Gateway:
            def __init__(self, model, usage, *args): self.usage = usage
            async def chat(self, client, state, messages, *args, **kwargs):
                self.usage(state.value, {'total_tokens': 25000, 'model': 'test', 'duration_ms': 1})
                return sequence.pop(0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / uuid.uuid4().hex; root.mkdir()
            def step(kind, label, detail, **metrics):
                if kind == 'state': machine.transition(AgentState(label)); states.append(label)
            with patch('agent.ensure_workspace', return_value=root), patch('agent.project_uid', return_value=os.getuid()), patch('agent.ModelGateway', Gateway), patch.dict(os.environ, {'AGENT_MAX_ITERATIONS': '4', 'AGENT_MAX_TOKENS': '100000'}):
                result = await run_agent(uuid.UUID(hex=root.name), 'Greeting', 'test', step, initial_tokens=150000, plan=json.dumps(cli_plan()))
            self.assertEqual(result['delivery'], 'demo')
            self.assertEqual(states[-1], 'PREVIEW_READY')
            ledger = json.loads((root/'.atoms/task-state.json').read_text())
            self.assertFalse(ledger['completed'])
            self.assertNotEqual(ledger['tasks'][0]['status'], 'done')
            from agent_session import session_status
            status = session_status(root)
            self.assertTrue(status['demo']['ready'])
            self.assertEqual(status['delivery_budget']['repair_iterations'], 2)
            self.assertEqual(status['delivery_budget']['repair_tokens'], 50000)
            self.assertEqual(status['delivery_budget']['task_tokens'], 150000)
            self.assertEqual(status['delivery_budget']['repair_token_limit'],50000)
            self.assertEqual(status['delivery_budget']['repair_iteration_limit'],2)
            self.assertIn('TODO', result['summary'])

    async def test_smoke_scene_is_required_in_stabilization_and_keeps_real_assertions(self):
        from test_agent_harness import example_plan
        from unittest.mock import AsyncMock
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root/'.atoms-workspace.json'
            config.write_text('{"build":"echo build"}')
            with patch('runtime.start_runtime', AsyncMock(return_value=object())), patch('agent_checks.browser_check', AsyncMock()) as check:
                code, _ = await verify_delivery(uuid.uuid4(), root, example_plan(), require_scenario=True)
                self.assertEqual(code, 1); check.assert_not_called()
            scenario = {'path': '/', 'actions': [{'action': 'click', 'selector': '#add'}, {'action': 'assert_text', 'selector': '#count', 'value': '1'}]}
            config.write_text(json.dumps({'build': 'echo build', 'demo': scenario}))
            with patch('runtime.start_runtime', AsyncMock(return_value=object())), patch('agent_checks.browser_check', AsyncMock(return_value=(0, '{"text":"Count 1"}'))) as check:
                code, _ = await verify_delivery(uuid.uuid4(), root, example_plan(), require_scenario=True)
                self.assertEqual(code, 0); self.assertEqual(check.call_args.args[2], scenario)

    async def test_invalid_workspace_config_is_repaired_instead_of_terminating(self):
        sequence = [calls(('write_files', {'files': [
            {'path': 'greet.py', 'content': 'print("hello")\n'},
            {'path': '.atoms-workspace.json', 'content': '[]'}]})),
            calls(('write_file', {'path': '.atoms-workspace.json', 'content': '{"test":"python greet.py"}'}))]
        class Gateway:
            def __init__(self, *args): pass
            async def chat(self, *args, **kwargs): return sequence.pop(0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/uuid.uuid4().hex; root.mkdir(); states = []
            with patch('agent.ensure_workspace', return_value=root), patch('agent.project_uid', return_value=os.getuid()), patch('agent.ModelGateway', Gateway):
                result = await run_agent(uuid.uuid4(), 'Greeting', 'test', lambda kind, label, *args, **kwargs: states.append(label),
                                         initial_tokens=20000000, plan=json.dumps(cli_plan()))
            self.assertEqual(result['delivery'], 'demo')
            self.assertIn('系统代码检查', states)
            self.assertIn('PREVIEW_READY', states)

    async def test_manual_stop_still_applies_after_limit(self):
        class Gateway:
            def __init__(self, *args): pass
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'.atoms').mkdir()
            with patch('agent.ensure_workspace', return_value=root), patch('agent.project_uid', return_value=os.getuid()), patch('agent.ModelGateway', Gateway):
                with self.assertRaises(AgentStopped):
                    await run_agent(uuid.uuid4(), 'Greeting', 'test', lambda *args, **kwargs: None,
                                    should_stop=lambda: True, initial_tokens=20000000, plan=json.dumps(cli_plan()))

    async def test_web_build_is_not_enough_blank_and_error_pages_are_rejected(self):
        from test_agent_harness import example_plan
        from unittest.mock import AsyncMock
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'.atoms-workspace.json').write_text('{"build":"echo build"}')
            for code, response in [(0, {'text': '', 'rendered_elements': 0}), (1, {'text': 'Hello', 'errors': ['crashed']})]:
                with patch('runtime.start_runtime', AsyncMock(return_value=object())), patch('agent_checks.browser_check', AsyncMock(return_value=(code, json.dumps(response)))):
                    result, _ = await verify_delivery(uuid.uuid4(), root, example_plan())
                    self.assertNotEqual(result, 0)
            with patch('runtime.start_runtime', AsyncMock(side_effect=RuntimeError('server died'))):
                result, report = await verify_delivery(uuid.uuid4(), root, example_plan())
                self.assertEqual(result, 1); self.assertIn('server died', report)


if __name__ == '__main__': unittest.main()
