"""Regression checks for the executor itself; no patched/mock dependencies."""
import json
import tempfile
import subprocess
import sys
import unittest
from pathlib import Path

from execution_guard import ExecutionGuard
from verification_policy import command_issue, inspect_sources, tool_issue


class VerificationPolicyTests(unittest.TestCase):
    def test_source_rewrites_and_passing_builds_cannot_reset_same_failure(self):
        state = {}
        for version in range(4):
            guard = ExecutionGuard(state)  # same durable state after a restart
            guard.validation_result('run_build', 0, 'Actual build passed')
            guard.validation_result('runtime_check', 0, 'Services ready')
            output = f'TestingLibraryElementError: Found multiple elements with the role "button"\nIndex.test.tsx:{17 + version}:253'
            guard.record(str(version), 'browser_check', {}, f'source-{version}', 1, output)
            guard.validation_result('browser_check', 1, output)
        self.assertEqual(state['verification_limits']['failures']['runtime'], 4)
        self.assertEqual(state['verification_limits']['pending_recovery']['tool'], 'browser_check')

    def test_different_failures_request_diagnosis_without_count_stop(self):
        guard = ExecutionGuard({})
        for index in range(30):
            guard.validation_result('runtime_check', 0, 'Ready')
            guard.validation_result('browser_check', 1, f'Error: different broken workflow {index}')
        self.assertEqual(guard.limits['failures']['runtime'], 30)
        self.assertIn('pending_recovery', guard.limits)

    def test_actual_success_allows_repair_but_unrelated_success_does_not(self):
        guard = ExecutionGuard({})
        for _ in range(3):
            guard.validation_result('run_build', 1, 'error TS2305: Missing export')
        guard.validation_result('run_build', 0, 'Compiled')
        guard.validation_result('run_build', 1, 'error TS2305: Missing export')
        self.assertEqual(guard.limits['failures']['run_build'], 4)

    def test_policy_rejections_request_contract_diagnosis_without_stopping(self):
        state = {}
        for _ in range(5):
            ExecutionGuard(state).policy_rejected('No unscoped test suite')
        self.assertEqual(state['verification_limits']['pending_recovery']['tool'], 'verification_policy')

    def test_no_progress_switches_diagnosis_instead_of_failing_by_count(self):
        guard = ExecutionGuard({})
        guard.observe('same', [])
        requests = sum(guard.observe('same', []) for _ in range(30))
        self.assertGreater(requests, 5)
        lenses = [guard.begin_recovery()['lens'] for _ in range(4)]
        self.assertEqual(len(set(lenses)), 4)

    def test_final_commands_reject_tests_but_local_scoped_tests_and_writes_are_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'frontend').mkdir()
            (root / 'package.json').write_text(json.dumps({'scripts': {'verify': 'npm --prefix frontend run verify'}}))
            (root / 'frontend/package.json').write_text(json.dumps({'scripts': {'verify': 'vitest run'}}))
            (root / 'check.py').write_text('import unittest\n')
            for command in ('npm test && python -m unittest', 'npx vitest run', 'npm run verify', 'python -m pytest', 'yarn test', 'cd frontend && npm run verify', 'npm --prefix=frontend run verify'):
                self.assertIsNotNone(command_issue(command, root), command)
            self.assertIsNone(tool_issue('run_shell', {'command': "python - <<'PY'\np='frontend/src/Index.test.tsx'\nopen(p,'w').write('test')\nPY"}, root))
            self.assertIsNone(tool_issue('run_shell', {'command': 'python app.py --check'}, root))
            self.assertIsNotNone(command_issue('python check.py', root))
            for command in ('npx vitest run frontend/src/Index.test.tsx', 'python -m pytest backend/tests/test_quota.py', 'python -m unittest tests.test_quota', 'npm run verify -- frontend/src/Index.test.tsx', 'python check.py', 'npm install --save-dev vitest', 'python -m pip install pytest'):
                self.assertIsNone(tool_issue('run_shell', {'command': command}, root), command)
                self.assertIsNotNone(command_issue(command, root), command)
            for command in ('npm test', 'npm run verify', 'pytest', 'vitest run --config vitest.config.ts', 'python -m unittest discover', 'npx vitest run frontend/src/Index.test.tsx && npm test'):
                self.assertIsNotNone(tool_issue('run_shell', {'command': command}, root), command)
            (root / '.atoms-workspace.json').write_text(json.dumps({'build': 'python -m pytest', 'dev': 'npm run verify'}))
            self.assertIsNotNone(tool_issue('run_build', {}, root))
            self.assertIsNotNone(tool_issue('runtime_check', {}, root))
            self.assertIsNotNone(tool_issue('http_request', {'path': '/api/data'}, root))
            self.assertIsNotNone(tool_issue('browser_check', {'actions': []}, root))
            (root / '.atoms-workspace.json').write_text(json.dumps({'dev': 'npm run dev', 'services': [{'command': 'python -m unittest'}]}))
            self.assertIsNotNone(tool_issue('runtime_check', {}, root))

    def test_file_tools_can_write_and_update_current_unit_tests(self):
        for name, args in (
            ('write_files', {'files': [{'path': 'frontend/src/Index.test.tsx', 'content': 'test'}]}),
            ('replace_in_file', {'path': 'backend/tests/test_quota.py', 'new': 'fixed'}),
            ('delete_file', {'path': 'backend/tests/test_quota.py'}),
            ('write_file', {'path': 'checks/check.py', 'content': 'from unittest.mock import patch'}),
            ('apply_patch', {'patch': '*** Begin Patch\n*** Update File: frontend/src/Index.test.tsx\n+test\n*** End Patch'}),
        ):
            self.assertIsNone(tool_issue(name, args, Path('.')))

    def test_actual_scoped_unit_command_runs_after_current_code_and_test_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = 'python -m unittest test_current'
            for value in (1, 2):
                (root / 'product.py').write_text(f'def current(): return {value}\n')
                (root / 'test_current.py').write_text(
                    'import unittest\nfrom product import current\n'
                    f'class CurrentTest(unittest.TestCase):\n def test_current(self): self.assertEqual(current(), {value})\n')
                self.assertIsNone(tool_issue('run_shell', {'command': command}, root))
                self.assertIsNotNone(command_issue(command, root))
                result = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'test_current'],
                                        cwd=root, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_real_source_checks_report_syntax_and_keep_existing_tests_out_of_gate(self):
        code, report = inspect_sources({'backend/app.py': 'def broken(:\n', 'package.json': '[]', 'backend/tests/test_old.py': 'invalid old test'})
        self.assertEqual(code, 1)
        self.assertIn('backend/app.py', report)
        self.assertIn('package.json', report)
        self.assertNotIn('test_old.py', report)
        self.assertEqual(inspect_sources({'app.py': 'print("actual program")', 'package.json': '{}'})[0], 0)


if __name__ == '__main__':
    unittest.main()
