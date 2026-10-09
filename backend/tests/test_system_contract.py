import copy
import json
import unittest
import tempfile
from pathlib import Path

from system_contract import (validate_system_contract, require_system_contract,
                             contract_acceptance_issues, confirmed_dependency_blocker,
                             diagnosis_fingerprint)
from runtime_assertions import shape_issues


def system_plan():
    return {'application_type': 'web', 'requirements': [{'id': 'R1'}],
            'system_contract': {'version': 1, 'journeys': [
                {'id': 'J1', 'requirement_ids': ['R1'], 'entry': 'Generate button',
                 'steps': ['Submit images → actual provider → outfits → read result'],
                 'success_signal': 'Three usable outfits', 'failure_signals': ['Configuration missing']}],
                'api_contracts': [{'method': 'POST', 'path': '/generate',
                    'request_media_type': 'multipart/form-data', 'request_fields': ['images'],
                    'success_status': 200, 'success_schema': {'type': 'object', 'required': ['outfits'],
                        'properties': {'outfits': {'type': 'array', 'minItems': 3,
                            'items': {'type': 'object', 'required': ['image'],
                                      'properties': {'image': {'type': 'string', 'minLength': 1}}}}}},
                    'error_statuses': [422, 503], 'requirement_ids': ['R1'],
                    'producer': 'backend/generate.py', 'consumers': ['frontend/api.ts']}],
                'dependencies': [{'name': 'Actual AI provider', 'requirement_ids': ['R1'],
                    'required_env': ['OPENAI_API_KEY'], 'binding': 'User supplied service key',
                    'probe': 'Actual generation request', 'on_unavailable': 'Await configuration'}],
                'invariants': ['Failed generation refunds quota'],
                'development_order': ['Provider readiness → vertical integration → remaining UI → acceptance']}}


def receipt(status=200, body=None, identifier='V1', source='current'):
    return {'id': identifier, 'kind': 'http_request', 'exit_code': 0,
            'source_digest': source, 'requirement_ids': ['R1'],
            'arguments': {'method': 'POST', 'path': '/generate', 'expect_schema': system_plan()['system_contract']['api_contracts'][0]['success_schema']},
            'output': json.dumps({'status': status, 'path': '/generate', 'method': 'POST',
                                  'body': json.dumps(body if body is not None else {'outfits': [{'image': '/image'}] * 3})})}


class SystemContractTests(unittest.TestCase):
    def test_local_auth_and_managed_database_are_agent_setup_not_external_todos(self):
        from system_contract import configuration_issue, defer_dependency, refresh_deferred_dependencies
        for name in ('AUTH_TOKEN_SECRET', 'JWT_SECRET', 'SESSION_SECRET', 'SECRET_KEY', 'APP_DATABASE_URL'):
            evidence = [receipt(503, {'detail': '未配置 ' + name})]
            issue = configuration_issue(evidence, 'current')
            self.assertEqual(issue['resolution'], 'auto_configure')
            self.assertIsNone(confirmed_dependency_blocker(evidence, 'current'))
            state = {'deferred_dependencies': [dict(issue, status='todo_configuration')]}
            self.assertFalse(defer_dependency(state, issue))
            refresh_deferred_dependencies(state, [], 'current')
            self.assertEqual(state['deferred_dependencies'], [])

    def test_mockable_service_is_not_external_and_successful_demo_clears_its_todo(self):
        from system_contract import configuration_issue, defer_dependency, refresh_deferred_dependencies
        missing = receipt(503, {'detail': '未配置 SMTP_PASSWORD'})
        issue = configuration_issue([missing], 'current')
        self.assertEqual(issue['resolution'], 'mock')
        self.assertIsNone(confirmed_dependency_blocker([missing], 'current'))
        state = {}
        self.assertTrue(defer_dependency(state, issue))
        demo = receipt(body={'simulated': True}, identifier='V2')
        refresh_deferred_dependencies(state, [missing, demo], 'current')
        self.assertEqual(state['deferred_dependencies'], [])
        self.assertIsNone(configuration_issue([missing, demo], 'current'))

    def test_explicit_mock_or_required_real_service_controls_provider_classification(self):
        from system_contract import configuration_issue
        plan = system_plan()
        missing = receipt(503, {'detail': '未配置 OPENAI_BASE_URL'})
        self.assertEqual(confirmed_dependency_blocker([missing], 'current')['required_env'], ['OPENAI_BASE_URL'])
        dependency = plan['system_contract']['dependencies'][0]
        dependency['required_env'].append('OPENAI_BASE_URL')
        dependency['demo_strategy'] = 'mock'
        self.assertIsNone(confirmed_dependency_blocker([missing], 'current', plan))
        self.assertEqual(configuration_issue([missing], 'current', plan)['resolution'], 'mock')
        dependency['real_service_required'] = True
        self.assertIsNotNone(confirmed_dependency_blocker([missing], 'current', plan))
        mixed = receipt(503, {'detail': '未配置 AUTH_TOKEN_SECRET 和 OPENAI_API_KEY'})
        self.assertEqual(confirmed_dependency_blocker([mixed], 'current')['required_env'], ['OPENAI_API_KEY'])

    def test_changing_existing_provider_plan_to_mock_clears_stale_external_todo(self):
        from system_contract import defer_dependency, refresh_deferred_dependencies
        missing = receipt(503, {'detail': '未配置 OPENAI_API_KEY'})
        state = {}
        defer_dependency(state, confirmed_dependency_blocker([missing], 'current'))
        plan = system_plan()
        plan['system_contract']['dependencies'][0]['demo_strategy'] = 'mock'
        refresh_deferred_dependencies(state, [receipt(body={'simulated': True})], 'current', plan)
        self.assertEqual(state['deferred_dependencies'], [])

    def test_handoff_does_not_mislabel_local_or_mock_todos_as_external(self):
        from system_contract import configuration_issue, delivery_todo
        from types import SimpleNamespace
        state = {'deferred_dependencies': [
            configuration_issue([receipt(503, {'detail': '未配置 AUTH_TOKEN_SECRET'})], 'current'),
            configuration_issue([receipt(503, {'detail': '未配置 SMTP_PASSWORD'})], 'current'),
            {'kind': 'api_probe', 'detail': 'Representative API requires repair'},
        ]}
        ledger = SimpleNamespace(plan=system_plan(), tasks=[], completion_issues=lambda files: [])
        with tempfile.TemporaryDirectory() as directory:
            todo = delivery_todo(Path(directory), ledger, state, {})
        self.assertNotIn('外部配置：', todo)
        self.assertIn('Agent 自动', todo)
        self.assertIn('mock/local adapter', todo)

    def test_external_dependency_is_deduplicated_todo_without_job_stop(self):
        from system_contract import defer_dependency
        blocker = confirmed_dependency_blocker([receipt(503, {'detail': '未配置 OPENAI_API_KEY'})], 'current')
        state = {'dependency_blocker': blocker}
        self.assertTrue(defer_dependency(state, blocker))
        self.assertFalse(defer_dependency(state, blocker))
        self.assertNotIn('dependency_blocker', state)
        self.assertEqual(len(state['deferred_dependencies']), 1)

    def test_simulation_keeps_configuration_todo_until_real_success(self):
        from system_contract import defer_dependency, refresh_deferred_dependencies
        state = {}
        defer_dependency(state, confirmed_dependency_blocker([receipt(503, {'detail': '未配置 OPENAI_API_KEY'})], 'current'))
        refresh_deferred_dependencies(state, [receipt(body={'simulated': True})], 'current')
        self.assertEqual(len(state['deferred_dependencies']), 1)
        refresh_deferred_dependencies(state, [receipt()], 'current')
        self.assertEqual(state['deferred_dependencies'], [])

    def test_deferred_task_unblocks_other_work_without_completing_requirement(self):
        from test_agent_harness import example_plan
        from agent_harness import TaskLedger
        plan = example_plan()
        plan['tasks'].append({**plan['tasks'][0], 'id': 'T2', 'title': 'UI and other features', 'depends_on': ['T1']})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = TaskLedger(root, plan, 'Demo')
            with self.assertRaises(ValueError):
                ledger.update('T1', 'deferred', '', [])
            ledger.update('T1', 'deferred', 'Demo adapter implemented; configure provider and verify real generation later', [])
            self.assertEqual(ledger.next_task()['id'], 'T2')
            self.assertFalse(ledger.completed)
            self.assertTrue(any('T1' in gap for gap in ledger.completion_issues({})))
            restored = TaskLedger(root, plan, 'Demo')
            self.assertEqual(restored.tasks[0]['status'], 'deferred')

    def test_simulated_http_success_is_demo_proof_not_real_provider_acceptance(self):
        from system_contract import success_receipt
        proof = receipt(body={'outfits': [{'image': '/image'}] * 3, 'simulated': True})
        self.assertFalse(success_receipt(proof))
        self.assertTrue(contract_acceptance_issues(system_plan(), [proof], 'current'))

    def test_fresh_web_plan_requires_contract_but_legacy_remains_readable(self):
        legacy = {'application_type': 'web', 'requirements': [{'id': 'R1'}]}
        self.assertEqual(validate_system_contract(legacy), legacy)
        with self.assertRaises(ValueError):
            require_system_contract(legacy)
        self.assertEqual(require_system_contract(system_plan()), system_plan())

    def test_plan_rejects_uncovered_requirements_and_error_as_success(self):
        for field in ('coverage', 'status', 'assertions', 'secret'):
            plan = system_plan()
            if field == 'coverage': plan['requirements'].append({'id': 'R2'})
            if field == 'status': plan['system_contract']['api_contracts'][0]['success_status'] = 503
            if field == 'assertions': plan['system_contract']['api_contracts'][0].pop('success_schema')
            if field == 'secret': plan['system_contract']['dependencies'][0]['required_env'] = ['sk-secret-value']
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_system_contract(plan)

    def test_success_must_prove_actual_payload_contract_and_current_source(self):
        self.assertEqual(contract_acceptance_issues(system_plan(), [receipt()], 'current'), [])
        for evidence in (receipt(body={'outfits': []}), receipt(status=503), receipt(source='old'),
                         receipt(body={'outfits': [{}, {}, {}]})):
            self.assertTrue(contract_acceptance_issues(system_plan(), [evidence], 'current'))
        unasserted = receipt()
        unasserted['arguments'].pop('expect_schema')
        self.assertTrue(contract_acceptance_issues(system_plan(), [unasserted], 'current'))

    def test_large_response_keeps_full_executor_assertions_after_context_truncation(self):
        evidence = receipt()
        response = json.loads(evidence['output'])
        response.update(body='{"outfits":[{"image":"' + 'large' * 1000,
                        passed=True, validated_schema=evidence['arguments']['expect_schema'])
        evidence['response'] = response
        self.assertFalse(contract_acceptance_issues(system_plan(), [evidence], 'current'))
        response['validated_schema'] = {'type': 'object'}
        self.assertTrue(contract_acceptance_issues(system_plan(), [evidence], 'current'))

    def test_configuration_blocker_requires_specific_actual_response_and_can_resolve(self):
        missing = receipt(503, {'detail': 'AI 服务未配置，请设置 OPENAI_API_KEY'})
        self.assertEqual(confirmed_dependency_blocker([missing], 'current')['required_env'], ['OPENAI_API_KEY'])
        self.assertIsNone(confirmed_dependency_blocker([missing], 'new'))
        self.assertIsNone(confirmed_dependency_blocker([receipt(503, {'detail': 'Provider temporarily unavailable'})], 'current'))
        self.assertIsNone(confirmed_dependency_blocker([missing, receipt(identifier='V2')], 'current'))

    def test_diagnosis_reuses_same_facts_despite_new_receipt_ids_and_task_notes(self):
        facts = {'source_digest': 'same', 'acceptance_gaps': ['R1'],
                 'tasks': [{'id': 'T1', 'status': 'pending', 'note': 'a'}],
                 'actual_failed_operations': [], 'recent_receipts': [{'id': 'V1', 'kind': 'http_request', 'result': 'Missing key'}]}
        changed = copy.deepcopy(facts)
        changed['tasks'][0]['note'] = 'Reworded diagnosis'
        changed['recent_receipts'][0]['id'] = 'V23'
        changed['recent_receipts'].append(copy.deepcopy(changed['recent_receipts'][0]))
        self.assertEqual(diagnosis_fingerprint(facts), diagnosis_fingerprint(changed))
        changed['source_digest'] = 'actual change'
        self.assertNotEqual(diagnosis_fingerprint(facts), diagnosis_fingerprint(changed))

    def test_shape_assertion_checks_all_items_and_rejects_empty_fake_success(self):
        schema = system_plan()['system_contract']['api_contracts'][0]['success_schema']
        self.assertTrue(shape_issues({'outfits': []}, schema))
        self.assertTrue(shape_issues({'outfits': [{'image': 'a'}, {'image': 'b'}, {'image': ''}]}, schema))
        self.assertFalse(shape_issues({'outfits': [{'image': 'a'}] * 3}, schema))
        with self.assertRaises(ValueError):
            shape_issues({}, {'execute': 'anything'})

    def test_task_cannot_finish_on_build_health_or_expected_rejection(self):
        from test_agent_harness import example_plan
        from agent_harness import TaskLedger, source_digest
        from agent import snapshot_files
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'frontend/src/pages').mkdir(parents=True)
            (root / 'frontend/src/pages/Calendar.tsx').write_text('export function Calendar() {}')
            (root / 'frontend/package.json').write_text('{}')
            (root / 'README.md').write_text('Start and verify')
            plan = example_plan()
            plan['system_contract'] = system_plan()['system_contract']
            ledger = TaskLedger(root, plan, 'Calendar')
            digest = source_digest(snapshot_files(root))
            for kind, status, path in (('run_build', 200, '/generate'), ('http_request', 200, '/health'), ('http_request', 503, '/generate')):
                result = ledger.record(kind, path, 0, json.dumps({'status': status, 'path': path}), digest, ['R1'])
                with self.subTest(kind=kind, status=status), self.assertRaises(ValueError):
                    ledger.update('T1', 'done', 'Not proof of business success', [result])
            proof = receipt()
            identifier = ledger.record('http_request', '/generate', 0, proof['output'], digest, ['R1'], proof['arguments'])
            ledger.update('T1', 'done', 'Actual success', [identifier])
            self.assertEqual(ledger.tasks[0]['status'], 'done')
