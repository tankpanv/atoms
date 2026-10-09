"""General execution regressions exposed by the persistence build incident."""
import copy
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import agent
from agent_checks import contains_json, http_request
from agent_harness import TaskLedger, source_digest
from agent_session import AgentSession, encoded, estimate_tokens, repair_tool_boundaries
from agent_delivery import DeliveryLimitReached
from coding_runtime import AgentState
from execution_guard import ExecutionGuard
from system_contract import contract_acceptance_issues, executable_api_contract, success_receipt
from test_agent_execution import cli_plan, calls
from test_system_contract import system_plan, receipt


class AssertionAndContractTests(unittest.TestCase):
    def test_empty_lists_assert_absence_and_boolean_is_not_integer(self):
        self.assertFalse(contains_json({'events': [{'id': 1}]}, {'events': []}))
        self.assertTrue(contains_json({'events': []}, {'events': []}))
        self.assertTrue(contains_json({'events': [{'id': 1, 'title': 'meeting'}]}, {'events': [{'id': 1}]}))
        self.assertFalse(contains_json({'ok': 1}, {'ok': True}))

    def test_old_vacuous_empty_list_receipt_is_no_longer_success(self):
        proof = receipt(body={'events': [{'id': 1}]})
        proof['arguments']['expect_json'] = {'events': []}
        self.assertFalse(success_receipt(proof))

    def test_dynamic_routes_runtime_prefixes_and_no_content_are_executable(self):
        plan = system_plan()
        api = plan['system_contract']['api_contracts'][0]
        api.update(method='PUT', path='/api/events/{event_id}', success_schema={'type': 'object', 'required': ['id']})
        proof = receipt(body={'id': 'record-1'})
        proof['arguments'].update(method='PUT', path='/api/runtime/current/api/events/record-1?view=full', expect_json={'id': 'record-1'})
        result = json.loads(proof['output'])
        result.update(method='PUT', path=proof['arguments']['path'])
        proof['output'] = json.dumps(result)
        self.assertFalse(contract_acceptance_issues(plan, [proof], 'current'))
        result['path'] = result['path'].split('?')[0] + '/foreign'
        proof['output'] = json.dumps(result)
        self.assertTrue(contract_acceptance_issues(plan, [proof], 'current'))
        api.update(method='DELETE', success_status=204, success_assertions={'body': 'empty'})
        api.pop('success_schema')
        proof['arguments'] = {'method': 'DELETE', 'path': '/api/events/record-1', 'expect_body': ''}
        proof['output'] = json.dumps({'method': 'DELETE', 'path': '/api/events/record-1', 'status': 204, 'body': '', 'passed': True})
        self.assertFalse(contract_acceptance_issues(plan, [proof], 'current'))
        proof['output'] = proof['output'].replace('"body": ""', '"body": "unexpected"')
        self.assertTrue(contract_acceptance_issues(plan, [proof], 'current'))

    def test_descriptive_assertions_become_notes_but_concrete_values_stay_binding(self):
        api = {'path': '/login', 'success_status': 200,
               'success_assertions': {'token_type': 'bearer', 'user.email': '已认证用户邮箱'},
               'success_schema': {'type': 'object', 'required': ['token_type', 'user']}}
        migrated = executable_api_contract(api)
        self.assertEqual(migrated['success_assertions'], {'token_type': 'bearer'})
        self.assertIn('user.email', migrated['success_notes'])
        self.assertEqual(api['success_assertions']['user.email'], '已认证用户邮箱')
        api.pop('success_schema')
        with self.assertRaisesRegex(ValueError, 'JSON'):
            executable_api_contract(api)

    def test_source_round_trips_do_not_reset_stall_counter(self):
        state = {}
        tasks = [{'id': 'T1', 'status': 'in_progress'}]
        ExecutionGuard(state).observe('A', tasks)
        ExecutionGuard(state).observe('B', tasks)
        with self.assertRaises(DeliveryLimitReached):
            for turn in range(20):
                ExecutionGuard(state).observe('A' if turn % 2 == 0 else 'B', tasks)

    def test_final_readback_uses_latest_state_and_discards_pre_mutation_reads(self):
        from agent_recovery import final_readback_requests
        def entry(sequence, method='GET', expected=None, code=0):
            return {'sequence': sequence, 'name': 'http_request', 'source': 'current', 'code': code,
                    'args': {'method': method, 'path': '/api/runtime/old/items', 'expect_status': 200,
                             'expect_json': expected, 'auth_from': {'response': 'alice'}, 'requirement_ids': ['R1']}}
        entries = [entry(1, expected=[{'id': 1}]), entry(2, method='DELETE'), entry(3, expected=[])]
        checks = final_readback_requests(entries, 'current')
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]['expect_json'], [])
        self.assertEqual(checks[0]['path'], '/items')
        self.assertFalse(final_readback_requests(entries[:2], 'current'))
        self.assertFalse(final_readback_requests(entries + [entry(4, expected=[], code=1)], 'current'))

    def test_readback_sequence_survives_reused_keys_and_legacy_state(self):
        from agent_recovery import final_readback_requests
        state = {'execution_attempts': {'old': {'name': 'http_request', 'code': 0, 'source': 'current',
                 'args': {'path': '/items', 'expect_status': 200, 'expect_json': [1], 'requirement_ids': ['R1']}}}}
        guard = ExecutionGuard(state)
        guard.record('write', 'http_request', {'method': 'POST', 'path': '/items'}, 'current', 0, '{}')
        self.assertFalse(final_readback_requests(list(guard.entries.values()), 'current'))
        guard.record('old', 'http_request', {'path': '/items', 'expect_status': 200, 'expect_json': [1, 2], 'requirement_ids': ['R1']}, 'current', 0, '{}')
        checks = final_readback_requests(list(ExecutionGuard(state).entries.values()), 'current')
        self.assertEqual(checks[0]['expect_json'], [1, 2])

    def test_prerequisite_can_close_with_stage_proof_but_final_owner_cannot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'greet.py').write_text('print("hello")')
            (root / 'README.md').write_text('Run python greet.py')
            plan = cli_plan()
            plan['system_contract'] = system_plan()['system_contract']
            plan['tasks'].append({**copy.deepcopy(plan['tasks'][0]), 'id': 'T2', 'depends_on': ['T1']})
            ledger = TaskLedger(root, plan, 'Complete the full product')
            source = source_digest(agent.snapshot_files(root))
            evidence = ledger.record('run_build', 'build frontend', 0, 'built', source, ['R1'])
            ledger.update('T1', 'done', 'Frontend stage compiled; API still belongs to T2', [evidence])
            self.assertEqual(ledger.next_task()['id'], 'T2')
            with self.assertRaisesRegex(ValueError, '真实成功链路'):
                ledger.update('T2', 'done', 'build alone', [evidence])
            self.assertTrue(ledger.completion_issues(agent.snapshot_files(root)))

    def test_task_revision_preserves_goals_and_evidence_and_is_atomic_on_invalid_graph(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = TaskLedger(Path(directory), cli_plan(), 'Keep the user goal')
            ledger.record('run_shell', 'python greet.py', 0, 'hello', 'source', ['R1'])
            goals = copy.deepcopy(ledger.plan['requirements'])
            tasks = [{**ledger.plan['tasks'][0], 'id': 'vertical', 'title': 'One complete vertical slice'}]
            ledger.revise_tasks(tasks, 'The old split blocked implementation before its dependency existed')
            self.assertEqual(ledger.plan['requirements'], goals)
            self.assertEqual(len(ledger.evidence), 1)
            saved = (ledger.directory / 'task-state.json').read_text()
            invalid = [{**tasks[0], 'requirement_ids': []}]
            with self.assertRaises(ValueError):
                ledger.revise_tasks(invalid, 'Cannot delete the requirements')
            self.assertEqual((ledger.directory / 'task-state.json').read_text(), saved)

    def test_contract_migration_preserves_resumable_task_and_session_state(self):
        from agent_session import digest
        from agent_harness import validate_plan
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = cli_plan()
            legacy['system_contract'] = system_plan()['system_contract']
            legacy['system_contract']['api_contracts'][0].update(success_assertions={'user.email': '已认证用户邮箱'})
            normalized = validate_plan(copy.deepcopy(legacy))
            ledger = TaskLedger(root, copy.deepcopy(normalized), 'Goal')
            ledger.tasks[0]['status'] = 'in_progress'
            ledger.persist()
            saved = json.loads((ledger.directory / 'task-state.json').read_text())
            saved['plan'] = legacy
            (ledger.directory / 'task-state.json').write_text(json.dumps(saved))
            resumed = TaskLedger(root, copy.deepcopy(normalized), 'Goal')
            self.assertTrue(resumed.resumed)
            self.assertEqual(resumed.tasks[0]['status'], 'in_progress')
            session = AgentSession(root)
            prefix = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'Goal'}]
            session.state = {'request': 'Goal', 'plan': legacy, 'plan_hash': digest(legacy), 'messages': prefix,
                             'system_hash': digest('system'), 'model': 'test', 'files': {}, 'completed': False}
            session.start('Goal', normalized, 'test', 'system', prefix, {})
            self.assertTrue(session.restored)

    def test_response_bindings_keep_opaque_values_out_of_model_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            session = AgentSession(Path(directory))
            opaque = uuid.uuid4().hex * 20
            session.http_response('alice', {'access_token': opaque, 'user': {'id': 'alice-id'}})
            session.http_response('nothing', None)
            self.assertIsNone(session.http_response('nothing'))
            raw = 'verification_id=V1\n' + json.dumps({'saved_as': 'alice', 'body': json.dumps({'access_token': opaque, 'user': {'id': 'alice-id'}})})
            rendered = session.output(calls(('http_request', {'path': '/login'}))['tool_calls'][0], raw)
            self.assertNotIn(opaque, rendered)
            self.assertIn('{{http:alice:/access_token}}', rendered)
            self.assertNotIn(opaque, encoded(session.http_bindings_context()))
            with session.connect() as conn:
                self.assertIn(opaque, conn.execute('SELECT result FROM outputs').fetchone()[0])


class HttpBindingTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_service_restart_replays_final_state_for_two_accounts(self):
        import runtime
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / uuid.uuid4().hex
            root.mkdir()
            project = uuid.UUID(hex=root.name)
            (root / 'README.md').write_text('Temporary authenticated persistence fixture')
            (root / '.atoms-workspace.json').write_text(json.dumps({'dev': 'python server.py'}))
            (root / 'server.py').write_text('''import os,json,sqlite3,secrets
from pathlib import Path
from http.server import BaseHTTPRequestHandler,HTTPServer
data=Path(os.environ['APP_DATA_DIR']);data.mkdir(exist_ok=True)
tokens=data/'tokens.json'
if not tokens.exists():tokens.write_text(json.dumps({name:secrets.token_urlsafe(160) for name in ['alice','bob']}))
auth=json.loads(tokens.read_text())
def db():
 c=sqlite3.connect(data/'items.db');c.execute('CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY,owner TEXT,title TEXT)');return c
class Handler(BaseHTTPRequestHandler):
 def reply(self,status,value):
  body=json.dumps(value).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
 def owner(self):return next((name for name,token in auth.items() if self.headers.get('Authorization')=='Bearer '+token),None)
 def do_GET(self):
  if self.path=='/':return self.reply(200,{'ready':True})
  owner=self.owner()
  if not owner:return self.reply(401,{'error':'unauthorized'})
  with db() as c:items=[{'id':row[0],'title':row[1]} for row in c.execute('SELECT id,title FROM items WHERE owner=? ORDER BY id',(owner,))]
  self.reply(200,{'items':items})
 def do_POST(self):
  body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  if self.path=='/login':return self.reply(200,{'access_token':auth[body['name']],'user':{'id':body['name']}})
  owner=self.owner()
  if not owner:return self.reply(401,{'error':'unauthorized'})
  with db() as c:identifier=c.execute('INSERT INTO items(owner,title) VALUES(?,?)',(owner,body['title'])).lastrowid
  self.reply(201,{'id':identifier,'title':body['title']})
 def do_DELETE(self):
  with db() as c:c.execute('DELETE FROM items WHERE owner=? AND id=?',(self.owner(),int(self.path.rsplit('/',1)[-1])))
  self.send_response(204);self.end_headers()
HTTPServer(('127.0.0.1',int(os.environ['PORT'])),Handler).serve_forever()
''')
            session = AgentSession(root)
            guard = ExecutionGuard(session.state)
            with patch('agent.ensure_workspace', return_value=root), patch('runtime.ensure_workspace', return_value=root), \
                 patch('agent.project_uid', return_value=os.getuid()), patch('runtime.project_uid', return_value=os.getuid()):
                try:
                    async def check(args):
                        code, output = await http_request(project, args)
                        source = source_digest(agent.snapshot_files(root))
                        guard.record(guard.key('http_request', args, source), 'http_request', args, source, code, output)
                        self.assertEqual(code, 0, output)
                    for name in ('alice', 'bob'):
                        await check({'path': '/login', 'method': 'POST', 'body': {'name': name}, 'save_as': name,
                                     'expect_status': 200, 'expect_json': {'user': {'id': name}}})
                    auth = {'response': 'alice'}
                    await check({'path': '/items', 'method': 'POST', 'body': {'title': 'temporary'}, 'auth_from': auth,
                                 'save_as': 'temporary', 'expect_status': 201, 'expect_json': {'title': 'temporary'}})
                    await check({'path': '/items', 'auth_from': auth, 'expect_status': 200, 'expect_json': {'items': [{'title': 'temporary'}]}, 'requirement_ids': ['R1']})
                    await check({'path': '/items/{{http:temporary:/id}}', 'method': 'DELETE', 'auth_from': auth, 'expect_status': 204, 'expect_body': ''})
                    await check({'path': '/items', 'auth_from': auth, 'expect_status': 200, 'expect_json': {'items': []}, 'requirement_ids': ['R1']})
                    await check({'path': '/items', 'method': 'POST', 'body': {'title': 'persistent'}, 'auth_from': auth, 'expect_status': 201, 'expect_json': {'title': 'persistent'}})
                    prefix = runtime.runtime_status(project)['url'].rstrip('/')
                    for name, expected in [('alice', [{'title': 'persistent'}]), ('bob', [])]:
                        await check({'path': prefix + '/items', 'auth_from': {'response': name}, 'expect_status': 200,
                                     'expect_json': {'items': expected}, 'requirement_ids': ['R1']})
                    code, report = await agent.verify_delivery_runtime(project, root,
                        {'application_type': 'service', 'architecture': {'backend': {'required': True}}},
                        require_scenario=True, session=session)
                    self.assertEqual(code, 0, report)
                    self.assertIn('persistent', report)
                    self.assertIn('"items":[]', report.replace(' ', '').replace('\\"', '"'))
                    self.assertNotEqual(runtime.runtime_status(project)['url'].rstrip('/'), prefix)
                finally:
                    await runtime.stop_runtime(project)

    async def test_two_accounts_reuse_exact_opaque_tokens_and_typed_ids_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = uuid.uuid4()
            tokens = {name: uuid.uuid4().hex * 25 for name in ('alice', 'bob')}
            seen = []
            def handler(request):
                if request.url.path == '/login':
                    name = json.loads(request.content)['name']
                    return httpx.Response(200, json={'access_token': tokens[name], 'user': {'id': 17 if name == 'alice' else 28}})
                seen.append((request.headers.get('authorization'), json.loads(request.content)))
                return httpx.Response(200, json={'events': [] if request.headers['authorization'] == 'Bearer ' + tokens['bob'] else [{'id': 17}]})
            real_client = httpx.AsyncClient
            def client(**kwargs):
                return real_client(transport=httpx.MockTransport(handler), **kwargs)
            runtime = SimpleNamespace(prefix='/api/runtime/local', port=12345, base_aware=False, services=[])
            with patch('agent.ensure_workspace', return_value=root), patch('agent_checks.start_runtime', AsyncMock(return_value=runtime)), patch('agent_checks.httpx.AsyncClient', side_effect=client):
                for name in ('alice', 'bob'):
                    code, output = await http_request(project, {'path': '/login', 'method': 'POST', 'body': {'name': name}, 'save_as': name,
                        'expect_status': 200, 'expect_schema': {'type': 'object', 'required': ['access_token', 'user']}})
                    self.assertEqual(code, 0, output)
                self.assertEqual(AgentSession(root).http_response('alice')['access_token'], tokens['alice'])
                for name in ('alice', 'bob'):
                    code, output = await http_request(project, {'path': '/events', 'method': 'POST', 'auth_from': {'response': name},
                        'body': {'user_id': '{{http:' + name + ':/user/id}}'}, 'expect_status': 200,
                        'expect_json': {'events': []} if name == 'bob' else {'events': [{'id': 17}]}})
                    self.assertEqual(code, 0, output)
                self.assertEqual(seen, [('Bearer ' + tokens['alice'], {'user_id': 17}), ('Bearer ' + tokens['bob'], {'user_id': 28})])
                code, output = await http_request(project, {'path': '/events', 'method': 'POST', 'body': {}, 'auth_from': {'response': 'alice'},
                    'expect_status': 200, 'expect_json': {'events': []}, 'save_as': 'bad_proof'})
                self.assertEqual(code, 1)
                self.assertTrue(json.loads(output)['assertion_errors'])
                with self.assertRaises(ValueError):
                    AgentSession(root).http_response('bad_proof')
            with tempfile.TemporaryDirectory() as other:
                with self.assertRaises(ValueError):
                    AgentSession(Path(other)).http_response('alice')


class AdaptiveExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_acting_model_revises_tasks_and_completes_without_independent_diagnosis(self):
        plan = cli_plan()
        new_tasks = [{**copy.deepcopy(plan['tasks'][0]), 'id': 'vertical', 'title': 'Implement and verify CLI together'}]
        sequence = [calls(('get_tasks', {})) for _ in range(5)] + [calls(('revise_tasks', {'reason': 'Resolve work as one complete slice', 'tasks': new_tasks})),
                    calls(('write_file', {'path': 'greet.py', 'content': 'print("hello")\n'}),
                          ('write_file', {'path': 'README.md', 'content': 'Run python greet.py'})),
                    calls(('run_shell', {'command': 'python greet.py', 'requirement_ids': ['R1']})),
                    calls(('update_task', {'id': 'vertical', 'status': 'done', 'evidence_ids': ['V1']}))]
        observed = []
        recovery_inputs = []
        class Gateway:
            def __init__(self, *args): pass
            async def chat(self, client, state, messages, *args, **kwargs):
                observed.append(state)
                recovery_inputs.extend(m['content'] for m in messages if isinstance(m.get('content'), str)
                                       and '直接根据以下事实统筹并执行' in m['content'])
                if state == AgentState.REVIEW:
                    return {'content': '{"approved":true,"issues":[],"summary":"verified"}'}
                if sequence:
                    return sequence.pop(0)
                return {'content': 'hello is implemented and verified'}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / uuid.uuid4().hex
            root.mkdir()
            with patch('agent.ensure_workspace', return_value=root), patch('agent.project_uid', return_value=os.getuid()), \
                 patch('agent.ModelGateway', Gateway), patch('model_catalog.catalog', return_value=[{'id': 'test', 'context': 128000}]):
                await agent.run_agent(uuid.UUID(hex=root.name), 'Create CLI', 'test', lambda *a, **kw: None, plan=json.dumps(plan))
            session = AgentSession(root)
            self.assertTrue(session.state['completed'])
            self.assertEqual(session.state['plan']['tasks'][0]['id'], 'vertical')
            self.assertNotIn(AgentState.PLAN, observed)
            self.assertTrue(recovery_inputs)

    def test_compaction_budget_does_not_restore_an_entire_source_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = TaskLedger(root, cli_plan(), 'Create CLI')
            session = AgentSession(root)
            prefix = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'Create CLI'}]
            session.start('Create CLI', ledger.plan, 'test', 'system', prefix, {})
            for i in range(8):
                session._remember_source(f'module{i}.py', 'value = 1\n' * 1800)
            messages = prefix + [{'role': 'assistant', 'content': 'old analysis ' * 12000}]
            reduced = session.compact(messages, 2, ledger, [], 'Keep the implementation goal', target_tokens=16000)
            self.assertLess(estimate_tokens(reduced), 16000)
            self.assertEqual(reduced, repair_tool_boundaries(reduced))
            self.assertEqual(len(session.state['working_set']), 6)  # archive remains larger than prompt
