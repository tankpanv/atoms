"""Real state and source checks for hypothesis-driven harness recovery."""
import json
import tempfile
import unittest
from pathlib import Path

from agent_harness import TaskLedger, source_digest
from agent_recovery import parse_recovery, recovery_facts, recovery_prompt
from execution_guard import ExecutionGuard
from test_agent_harness import example_plan


class RecoveryLogicTests(unittest.TestCase):
    def test_repeated_completion_gaps_rotate_hypotheses_and_survive_resume(self):
        state = {}
        for index in range(20):
            guard = ExecutionGuard(state)
            for _ in range(2):
                guard.validation_result('completion_check', 1, 'R2 缺少当前源码的 api 验收证据')
            incident = guard.begin_recovery()
            self.assertEqual(incident['round'], index + 1)
            if index:
                self.assertTrue(incident['previous_attempts'])
            diagnosis = {'root_cause': '证据缺口未证明业务错误', 'hypotheses': [
                {'cause': '陈旧证据', 'probe': '对当前源码执行 GET /api/events，传 R2', 'supports': '实际返回成功', 'refutes': '实际业务错误'},
                {'cause': '接口失败', 'probe': '检查响应体并沿路由追踪', 'supports': '业务请求失败', 'refutes': '预期响应通过'}],
                'next_actions': ['执行实际接口检查，关联 R2', '根据实际差异修复并回读']}
            guard.recovery_diagnosed(incident, parse_recovery(json.dumps(diagnosis)))
        self.assertEqual(len({r['lens'] for r in state['verification_limits']['recoveries']}), 4)
        self.assertEqual(state['verification_limits']['recoveries'][-1]['status'], 'awaiting_real_probe_and_repair')

    def test_fresh_diagnosis_receives_actual_probe_and_repair_outcomes(self):
        guard = ExecutionGuard({})
        incident = guard.begin_recovery()
        guard.recovery_diagnosed(incident, {'root_cause': 'Request encoding is not yet established'})
        args = {'path': '/form', 'method': 'POST', 'body': {'occasion': 'daily'}}
        guard.recovery_action('http_request', args, 1, '{"status":422,"expected_status":200,"body":"occasion missing"}')
        guard.recovery_action('http_request', args, 1, '{"status":422,"expected_status":200,"body":"occasion missing"}')
        self.assertEqual(len(incident['observations']), 1)
        self.assertEqual(incident['status'], 'hypothesis_needs_revision')
        next_incident = guard.begin_recovery()
        self.assertIn('occasion missing', str(next_incident['previous_attempts']))
        guard.recovery_action('http_request', {'path': '/form', 'form': {'occasion': 'daily'}}, 0, 'actual request passed')
        self.assertEqual(next_incident['status'], 'probe_passed')

    def test_same_failed_call_is_suppressed_until_a_new_real_probe_passes(self):
        guard = ExecutionGuard({})
        args = {'path': '/form', 'body': {'occasion': 'daily'}}
        key = guard.key('http_request', args, 'source')
        for _ in range(2):
            guard.record(key, 'http_request', args, 'source', 1, 'Actual 422; expected 200')
        self.assertIsNotNone(guard.prior(key))
        guard.begin_recovery()
        guard.recovery_action('read_file', {'path': 'route.py'}, None, 'Known route source')
        self.assertIsNotNone(guard.prior(key))
        guard.recovery_action('http_request', {'path': '/openapi.json'}, 0, 'Actual requestBody multipart/form-data')
        self.assertIsNone(guard.prior(key))
        guard.record(key, 'http_request', args, 'source', 1, 'Still actual 422; expected 200')
        self.assertIsNotNone(guard.prior(key))

    def test_expected_422_is_success_fact_not_business_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'README.md').write_text('Actual startup and core operation')
            files = {'README.md': 'Actual startup and core operation'}
            ledger = TaskLedger(root, example_plan(), 'Calendar')
            report = json.dumps({'status': 422, 'expected_status': 422, 'passed': True, 'body': 'missing upload'})
            ledger.record('http_request', '/api/upload', 0, report, source_digest(files), ['R1'])
            guard = ExecutionGuard({})
            guard.validation_result('http_request', 0, report)
            facts = recovery_facts(root, ledger, [], source_digest(files), {'running': True}, guard.failures())
            self.assertEqual(facts['actual_failed_operations'], [])
            self.assertIn('422', facts['recent_receipts'][0]['result'])
            self.assertTrue(facts['recent_receipts'][0]['current_source'])
            self.assertTrue(facts['acceptance_gaps'])
            prompt = recovery_prompt(guard.begin_recovery())
            self.assertIn('port_env', prompt)
            self.assertIn('期望且通过', prompt)

    def test_a_claimed_root_cause_without_discriminating_probes_is_rejected(self):
        for diagnosis in ({'root_cause': 'Guess', 'next_actions': ['rewrite everything']},
                          {'root_cause': 'Guess', 'hypotheses': [{'cause': 'missing file'}], 'next_actions': ['retry']}):
            with self.assertRaises(ValueError):
                parse_recovery(json.dumps(diagnosis))
