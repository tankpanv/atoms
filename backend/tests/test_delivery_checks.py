import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agent import TOOLS, verify_delivery
from agent_session import AgentSession
from delivery_checks import resolve_scene, validate_scene
from test_agent_harness import example_plan


class SceneTests(unittest.IsolatedAsyncioTestCase):
    def test_generic_workflow_rejects_form_echo_and_fullstack_without_api(self):
        plan=example_plan();plan['architecture']['backend']['required']=True
        with self.assertRaisesRegex(ValueError,'assert_response'):
            validate_scene({'actions':[{'action':'assert_visible','selector':'#dashboard'}]},TOOLS,plan=plan)
        with self.assertRaisesRegex(ValueError,'业务结果'):
            validate_scene({'actions':[{'action':'fill','selector':'#title','value':'Draft'},
                                      {'action':'assert_value','selector':'#title','value':'Draft'}]},TOOLS)
        with self.assertRaisesRegex(ValueError,'提交'):
            validate_scene({'actions':[{'action':'fill','selector':'#title','value':'Draft'},
                                      {'action':'assert_visible','selector':'#existing-list'}]},TOOLS)
        scene={'actions':[{'action':'fill','selector':'#title','value':'Draft'},
                          {'action':'click','selector':'#save'},
                          {'action':'assert_response','selector':'/api/items','method':'POST','status':201,'expect_json':{'title':'Draft'}},
                          {'action':'assert_text','selector':'#list','value':'Draft'},
                          {'action':'reload'},
                          {'action':'assert_response','selector':'/api/items','expect_json':[{'title':'Draft'}]},
                          {'action':'assert_text','selector':'#list','value':'Draft'}]}
        self.assertEqual(validate_scene(scene,TOOLS,plan=plan)['actions'],scene['actions'])

    async def test_configured_core_contract_runs_without_scene_model_call(self):
        scene={'actions':[{'action':'fill','selector':'#email','value':'isolated@example.test'},
                          {'action':'click','selector':'#submit'},
                          {'action':'assert_text','selector':'#library','value':'Saved words'}]}
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'.atoms-workspace.json').write_text(json.dumps({'demo':scene}))
            gateway=type('Gateway',(),{'chat':AsyncMock()})()
            result=await resolve_scene(root,example_plan(),{'text':'Register'},AgentSession(root),gateway,None,TOOLS)
            self.assertEqual(result['actions'],scene['actions'])
            gateway.chat.assert_not_awaited()

    def test_answer_game_cannot_pass_with_visible_feedback_only(self):
        actions = [{'action':'click','selector':'#hint'},
                   {'action':'fill_from_text','selector':'#input','source_selector':'#answer'},
                   {'action':'click','selector':'#submit'},
                   {'action':'assert_visible','selector':'#feedback'},
                   {'action':'fill','selector':'#input','value':'24'},
                   {'action':'click','selector':'#submit'},
                   {'action':'assert_visible','selector':'#feedback'}]
        with self.assertRaisesRegex(ValueError,'分别 assert_text'):
            validate_scene({'actions':actions},TOOLS,True)
        actions[3] = {'action':'assert_text','selector':'#feedback','value':'回答正确'}
        actions[6] = {'action':'assert_text','selector':'#feedback','value':'四个数字'}
        self.assertEqual(validate_scene({'actions':actions},TOOLS,True)['actions'],actions)

    def test_real_failed_job_keyboard_submission_is_valid(self):
        actions = [{'action':'click','selector':'#hint'},
                   {'action':'fill_from_text','selector':'#input','source_selector':'#answer'},
                   {'action':'press','selector':'#input','value':'Enter'},
                   {'action':'assert_text','selector':'#feedback','value':'正确'},
                   {'action':'fill','selector':'#input','value':'1+1+1+1'},
                   {'action':'press','selector':'#input','value':'Enter'},
                   {'action':'assert_text','selector':'#feedback','value':'每张牌'}]
        self.assertEqual(validate_scene({'actions':actions},TOOLS,True)['actions'],actions)
        actions[5]={'action':'click','selector':'#submit'}
        self.assertEqual(validate_scene({'actions':actions},TOOLS,True)['actions'],actions)
        actions[2]['selector']='#different-input'
        with self.assertRaises(ValueError): validate_scene({'actions':actions},TOOLS,True)
        actions[2]['selector']='#input';actions[2]['value']='Tab'
        with self.assertRaises(ValueError): validate_scene({'actions':actions},TOOLS,True)

    async def test_preview_only_never_replays_broken_proposed_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'.atoms-workspace.json').write_text(json.dumps({'build':'echo build','demo':{'actions':[{'action':'click','selector':'#does-not-exist'}]}}))
            plan=example_plan(); resolver=AsyncMock()
            with patch('runtime.start_runtime',AsyncMock(return_value=object())), patch('agent_checks.browser_check',AsyncMock(return_value=(0,'{"text":"Actual product","rendered_elements":4}'))) as check:
                code,_=await verify_delivery(uuid.uuid4(),root,plan,scene_resolver=resolver,preview_only=True)
            self.assertEqual(code,0);resolver.assert_not_called()
            self.assertTrue(all(c.args[2]['actions']==[{'action':'assert_visible','selector':'body'}] for c in check.call_args_list))

    async def test_preview_only_rejects_blank_template_and_runtime_errors(self):
        for result in [(0,'{"text":"","rendered_elements":0}'),(0,'{"text":"Vite + React Click on the Vite and React logos"}'),(1,'{"text":"Game","errors":["Uncaught TypeError"]}')]:
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory);(root/'.atoms-workspace.json').write_text('{"build":"echo build"}')
                with patch('runtime.start_runtime',AsyncMock(return_value=object())),patch('agent_checks.browser_check',AsyncMock(return_value=result)):
                    code,_=await verify_delivery(uuid.uuid4(),root,example_plan(),preview_only=True)
                self.assertNotEqual(code,0)

    async def test_browser_feedback_regrounds_expectation_once_without_code_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'.atoms-workspace.json').write_text('{"build":"echo build"}')
            wrong = {'actions':[{'action':'assert_text','selector':'#feedback','value':'错误'}]}
            corrected = {'actions':[{'action':'assert_text','selector':'#feedback','value':'四个数字'}]}
            resolver = AsyncMock(side_effect=[wrong,corrected])
            failure = {'error':'Expected 错误; actual 请将四个数字各使用一次', 'text':'请将四个数字各使用一次', 'errors':[], 'failed_responses':[]}
            with patch('runtime.start_runtime', AsyncMock(return_value=object())), patch('agent_checks.browser_check', AsyncMock(side_effect=[
                (0,'{"text":"Game","controls":[]}'),(1,json.dumps(failure)),(0,'{"text":"请将四个数字各使用一次"}')])):
                code,_ = await verify_delivery(uuid.uuid4(),root,example_plan(),True,resolver)
            self.assertEqual(code,0)
            self.assertEqual(resolver.await_count,2)
            self.assertEqual(resolver.call_args.args[0]['scene_failure']['result'],failure)

    async def test_three_mismatched_scenes_stop_instead_of_repairing_business_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'.atoms-workspace.json').write_text('{"build":"echo build"}')
            scene = {'actions':[{'action':'assert_text','selector':'#feedback','value':'错误'}]}
            resolver = AsyncMock(return_value=scene)
            failure = {'error':'Expectation mismatch','errors':[],'failed_responses':[]}
            with patch('runtime.start_runtime', AsyncMock(return_value=object())), patch('agent_checks.browser_check', AsyncMock(side_effect=[
                (0,'{"text":"Game"}'),(1,json.dumps(failure)),(1,json.dumps(failure)),(1,json.dumps(failure))])):
                with self.assertRaisesRegex(RuntimeError,'不自动反复修改'):
                    await verify_delivery(uuid.uuid4(),root,example_plan(),True,resolver)
            self.assertEqual(resolver.await_count,3)

    async def test_real_page_exception_does_not_become_scene_only_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'.atoms-workspace.json').write_text('{"build":"echo build"}')
            resolver = AsyncMock(return_value={'actions':[{'action':'assert_visible','selector':'#game'}]})
            failure = {'error':'Locator timeout','errors':['Uncaught TypeError: invalid game state']}
            with patch('runtime.start_runtime', AsyncMock(return_value=object())), patch('agent_checks.browser_check', AsyncMock(side_effect=[
                (0,'{"text":"Game"}'),(1,json.dumps(failure))])):
                code,_ = await verify_delivery(uuid.uuid4(),root,example_plan(),True,resolver)
            self.assertEqual(code,1)
            resolver.assert_awaited_once()

    async def test_failed_cached_scene_is_regenerated_from_actual_feedback(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); session=AgentSession(root)
            good={'actions':[{'action':'assert_text','selector':'#feedback','value':'四个数字'}]}
            gateway=type('Gateway',(),{'chat':AsyncMock(return_value={'content':json.dumps(good)})})()
            await resolve_scene(root,example_plan(),{},session,gateway,None,TOOLS)
            await resolve_scene(root,example_plan(),{'scene_failure':{'error':'wrong label'}},session,gateway,None,TOOLS)
            self.assertEqual(gateway.chat.await_count,2)
            self.assertIn('wrong label',gateway.chat.call_args.args[2][1]['content'])

    async def test_reasoning_starvation_changes_budget_and_persists_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scene = {'actions':[{'action':'assert_text','selector':'#result','value':'Correct'}]}
            gateway = type('Gateway', (), {'chat':AsyncMock(side_effect=[
                {'content':'', '_response_meta':{'finish_reason':'length', 'completion_tokens':8192,
                    'reasoning_tokens':8192, 'content_chars':0, 'max_tokens':8192}},
                {'content':json.dumps(scene), '_response_meta':{'finish_reason':'stop'}}])})()
            session = AgentSession(root)
            self.assertEqual(await resolve_scene(root, example_plan(), {'text':'Game'}, session, gateway, None, TOOLS), scene)
            calls = gateway.chat.call_args_list
            self.assertEqual([call.kwargs['max_tokens'] for call in calls], [8192,16384])
            self.assertEqual(calls[0].kwargs['reasoning'], {'effort':'low'})
            with session.connect() as conn:
                phase = json.loads(conn.execute("SELECT data FROM phases WHERE name='delivery_scene'").fetchone()[0])
            self.assertEqual(phase['attempts'][0]['reasoning_tokens'],8192)
            self.assertIn('截断',phase['attempts'][0]['error'])
            self.assertTrue(phase['completed'])

    async def test_twice_truncated_valid_json_is_not_silently_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            response = {'content':'{"actions":[{"action":"assert_visible","selector":"#result"}]}',
                        '_response_meta':{'finish_reason':'length','reasoning_tokens':2000,'content_chars':70}}
            gateway = type('Gateway', (), {'chat':AsyncMock(return_value=response)})()
            session = AgentSession(root)
            with self.assertRaisesRegex(RuntimeError,'截断.*思考 token=2000'):
                await resolve_scene(root, example_plan(), {}, session, gateway, None, TOOLS)
            self.assertEqual(gateway.chat.await_count,2)
            with session.connect() as conn:
                phase = json.loads(conn.execute("SELECT data FROM phases WHERE name='delivery_scene'").fetchone()[0])
            self.assertEqual(len(phase['attempts']),2)
            self.assertNotIn('scene',phase)

    async def test_valid_planner_scene_is_grounded_in_random_page_and_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'.atoms-workspace.json').write_text('{"build":"echo build"}')
            plan = example_plan()
            plan['delivery_checks'] = {'actions':[{'action':'fill','selector':'#answer','value':'(12-8)*(7-1)'}, {'action':'assert_text','selector':'#result','value':'Correct'}]}
            grounded = {'actions':[{'action':'click','selector':'#hint'}, {'action':'fill_from_text','selector':'#answer','source_selector':'#solution'}, {'action':'click','selector':'#submit'}, {'action':'assert_text','selector':'#result','value':'Correct'}]}
            gateway = type('Gateway', (), {'chat':AsyncMock(return_value={'content':json.dumps(grounded)})})()
            async def resolver(observed):
                return await resolve_scene(root, plan, observed, AgentSession(root), gateway, None, TOOLS)
            with patch('runtime.start_runtime', AsyncMock(return_value=object())), patch('agent_checks.browser_check', AsyncMock(side_effect=[(0,'{"text":"Random cards 12 13 7 1"}'),(0,'{"text":"Correct"}')])) as check:
                code, _ = await verify_delivery(uuid.uuid4(), root, plan, True, resolver)
                self.assertEqual(code,0)
                self.assertEqual(check.call_args_list[0].args[2]['actions'], [{'action':'assert_visible','selector':'body'}])
                self.assertEqual(check.call_args.args[2]['actions'],grounded['actions'])
            await resolver({'text':'Different random cards'})
            gateway.chat.assert_awaited_once()
            self.assertIn('proposed_scene',gateway.chat.call_args.args[2][1]['content'])

    async def test_assertion_timeout_is_not_reported_as_blank_page(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'.atoms-workspace.json').write_text('{"build":"echo build"}')
            with patch('runtime.start_runtime', AsyncMock(return_value=object())), patch('agent_checks.browser_check', AsyncMock(return_value=(1,'{"error":"expected Correct, received Wrong"}'))):
                code, report = await verify_delivery(uuid.uuid4(),root,example_plan())
                self.assertEqual(code,1)
                self.assertNotIn('没有可观察',report)

    async def test_missing_metadata_uses_observed_controls_once_and_executes_real_scene(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'.atoms-workspace.json').write_text('{"build":"echo build"}')
            session = AgentSession(root)
            observed = {'text':'Counter 0', 'controls':[{'id':'add','text':'Add'}]}
            scene = {'actions':[{'action':'click','selector':'#add'},
                                {'action':'assert_text','selector':'#count','value':'1'}]}
            gateway = type('Gateway', (), {'chat':AsyncMock(return_value={'content':json.dumps(scene)})})()
            async def resolver(page):
                return await resolve_scene(root, example_plan(), page, session, gateway, None, TOOLS)
            with patch('runtime.start_runtime', AsyncMock(return_value=object())), \
                 patch('agent_checks.browser_check', AsyncMock(side_effect=[(0,json.dumps(observed)), (0,'{"text":"Counter 1"}')])) as check:
                code, _ = await verify_delivery(uuid.uuid4(), root, example_plan(), True, resolver)
                self.assertEqual(code, 0)
                self.assertEqual(check.call_args.args[2]['actions'], scene['actions'])
            # Resume reuses the harness contract; it does not ask the engineer
            # to change .atoms-workspace.json or reread the application.
            await resolve_scene(root, example_plan(), observed, AgentSession(root), gateway, None, TOOLS)
            gateway.chat.assert_awaited_once()
            self.assertIn('Counter 0', gateway.chat.call_args.args[2][1]['content'])
            self.assertNotIn('demo', json.loads((root/'.atoms-workspace.json').read_text()))

    async def test_prose_planner_checks_fall_back_to_observed_scene_without_code_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'.atoms-workspace.json').write_text('{"build":"echo build"}')
            plan = example_plan(); plan['delivery_checks'] = {'actions':['Click the add button']}
            scene = {'actions':[{'action':'click','selector':'#add'}, {'action':'assert_text','selector':'#count','value':'1'}]}
            gateway = type('Gateway', (), {'chat':AsyncMock(return_value={'content':json.dumps(scene)})})()
            async def resolver(observed):
                return await resolve_scene(root, plan, observed, AgentSession(root), gateway, None, TOOLS)
            with patch('runtime.start_runtime', AsyncMock(return_value=object())), \
                 patch('agent_checks.browser_check', AsyncMock(side_effect=[(0,'{"text":"Counter 0"}'),(0,'{"text":"Counter 1"}')])):
                code, _ = await verify_delivery(uuid.uuid4(), root, plan, True, resolver)
                self.assertEqual(code, 0)
                gateway.chat.assert_awaited_once()

    def test_setup_dependency_does_not_block_its_own_future_game_acceptance(self):
        from agent_harness import validate_plan
        plan = example_plan()
        first = plan['tasks'][0]
        first['files'] = ['frontend/', 'package.json', '.atoms-workspace.json']
        product = {**first, 'id':'T2', 'title':'Implement gameplay', 'depends_on':[first['id']], 'files':['frontend/src/game.ts']}
        plan['tasks'] = [first, product]
        normalized = validate_plan(plan)
        self.assertEqual(len(normalized['tasks']), 1)
        self.assertEqual(normalized['tasks'][0]['depends_on'], [])
        self.assertEqual(set(normalized['tasks'][0]['requirement_ids']), set(first['requirement_ids']))

    async def test_install_cache_invalidates_on_manifest_changes_and_failed_install(self):
        from agent import ensure_npm_dependencies
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'package.json').write_text('{"name":"test"}')
            async def install(*args):
                (root/'node_modules').mkdir(exist_ok=True)
                (root/'node_modules/.package-lock.json').write_text('{}')
                return 0, 'installed'
            with patch('agent.ensure_workspace', return_value=root), patch('agent.run_command', AsyncMock(side_effect=install)) as run:
                await ensure_npm_dependencies(uuid.uuid4())
                await ensure_npm_dependencies(uuid.uuid4())
                self.assertEqual(run.await_count,1)
                (root/'package.json').write_text('{"name":"changed"}')
                await ensure_npm_dependencies(uuid.uuid4())
                self.assertEqual(run.await_count,2)
                (root/'node_modules/.package-lock.json').unlink()
                await ensure_npm_dependencies(uuid.uuid4())
                self.assertEqual(run.await_count,3)
            (root/'package.json').write_text('{"name":"failure"}')
            with patch('agent.ensure_workspace', return_value=root), patch('agent.run_command', AsyncMock(return_value=(1,'failed'))) as run:
                await ensure_npm_dependencies(uuid.uuid4())
                await ensure_npm_dependencies(uuid.uuid4())
                self.assertEqual(run.await_count,2)

    async def test_large_lockfile_hashes_all_bytes_without_using_model_read_limit(self):
        from agent import ensure_npm_dependencies, MAX_FILE_BYTES, read_file
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'package.json').write_text('{"name":"large-lock"}')
            lock = root/'package-lock.json'
            lock.write_text(' ' * (MAX_FILE_BYTES + 65536) + '{}')
            with self.assertRaisesRegex(ValueError, '文件过大'):
                read_file(root, 'package-lock.json')
            async def install(*args):
                (root/'node_modules').mkdir(exist_ok=True)
                (root/'node_modules/.package-lock.json').write_text('{}')
                return 0, 'installed'
            with patch('agent.ensure_workspace', return_value=root), patch('agent.run_command', AsyncMock(side_effect=install)) as run:
                self.assertEqual((await ensure_npm_dependencies(uuid.uuid4()))[0], 0)
                await ensure_npm_dependencies(uuid.uuid4())
                self.assertEqual(run.await_count, 1)
                # Change beyond the first chunk, keeping file size unchanged.
                lock.write_text(' ' * (MAX_FILE_BYTES + 65536) + '[]')
                await ensure_npm_dependencies(uuid.uuid4())
                self.assertEqual(run.await_count, 2)

    async def test_configured_npm_build_installs_through_verified_dependency_cache(self):
        from agent import run_build
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'package.json').write_text('{"scripts":{"build":"vite build"}}')
            (root/'.atoms-workspace.json').write_text('{"build":"npm run build"}')
            with patch('agent.ensure_workspace', return_value=root), \
                 patch('agent.ensure_npm_dependencies', AsyncMock(return_value=(0,'installed'))) as install, \
                 patch('agent.run_shell', AsyncMock(return_value=(0,'built'))) as build:
                self.assertEqual(await run_build(uuid.uuid4()), (0,'built'))
                install.assert_awaited_once(); build.assert_awaited_once()

    async def test_bad_contract_and_provider_failure_do_not_become_business_repair_loops(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for failure in (None, RuntimeError('provider 403')):
                gateway = type('Gateway', (), {'chat':AsyncMock(return_value={'content':'{}'}, side_effect=failure)})()
                with self.assertRaises(RuntimeError):
                    await resolve_scene(root, example_plan(), {'text':'Counter'}, AgentSession(root), gateway, None, TOOLS)
                self.assertLessEqual(gateway.chat.await_count, 2)

    async def test_failed_functional_assertion_is_not_accepted_as_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'.atoms-workspace.json').write_text('{"build":"echo build"}')
            plan = example_plan(); plan['delivery_checks'] = {'actions':[{'action':'assert_text','selector':'#count','value':'1'}]}
            with patch('runtime.start_runtime', AsyncMock(return_value=object())), \
                 patch('agent_checks.browser_check', AsyncMock(return_value=(1,'{"text":"Counter 0","error":"expected 1"}'))):
                code, _ = await verify_delivery(uuid.uuid4(), root, plan, True)
                self.assertNotEqual(code, 0)
