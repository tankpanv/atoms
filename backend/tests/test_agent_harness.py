import copy
import json
import tempfile
import unittest
from pathlib import Path

from agent_harness import TaskLedger, compact_messages, resumable_plan, source_digest, validate_plan
from coding_runtime import configured_tests, review_sources


def example_plan():
    return {
        "goal": "A calendar", "application_type": "web",
        "architecture": {"frontend": {"stack": "React", "directory": "frontend"},
                         "backend": {"required": False, "reason": "Explicit local-only calendar", "stack": "none", "directory": "backend"}},
        "design": "Calendar domain separated from view components",
        "commands": {"bootstrap": [], "build": [], "test": [], "dev": "npm run dev"},
        "requirements": [{"id": "R1", "description": "Create events", "acceptance": ["Add event, then reload and find it"], "verification": "browser"}],
        "tasks": [{"id": "T1", "title": "Calendar", "requirement_ids": ["R1"], "depends_on": [], "files": ["frontend/src/pages/Calendar.tsx"], "verification": "Browser event creation and reload"}],
    }


class HarnessTests(unittest.TestCase):
    def test_resume_keeps_progress_but_requires_fresh_real_acceptance(self):
        ledger = TaskLedger(self.root, example_plan(), 'Calendar')
        receipt = ledger.record('browser_check', '/', 0, 'passed', source_digest(self.files), ['R1'])
        ledger.update('T1', 'done', 'Implemented', [receipt])
        restored = resumable_plan(self.root, '继续')
        self.assertEqual(restored[0], 'Calendar')
        resumed = TaskLedger(self.root, restored[1], restored[0])
        self.assertTrue(resumed.resumed)
        self.assertEqual(resumed.tasks[0]['status'], 'done')
        self.assertTrue(any('验收证据' in item for item in resumed.completion_issues(self.files)))
        resumed.record('browser_check', '/', 0, 'rechecked', source_digest(self.files), ['R1'])
        self.assertEqual(resumed.completion_issues(self.files), [])
        resumed.completed = True
        resumed.persist()
        self.assertIsNone(resumable_plan(self.root, '继续'))

    def test_new_requirements_do_not_silently_reuse_an_old_plan(self):
        TaskLedger(self.root, example_plan(), 'Calendar')
        self.assertIsNone(resumable_plan(self.root, '继续，并增加用户登录'))
        self.assertIn('frontend/src/pages/Calendar.tsx', review_sources(self.files, self.files))
        after = {**self.files, 'frontend/index.html': '<title>Calendar</title>'}
        self.assertIn('frontend/src/pages/Calendar.tsx', review_sources(self.files, after, full=True))

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "frontend/src/pages").mkdir(parents=True)
        (self.root / "frontend/package.json").write_text('{}')
        (self.root / "README.md").write_text("Install, start and verify")
        self.files = {"README.md": "Install, start and verify", "frontend/package.json": "{}",
                      "frontend/src/pages/Calendar.tsx": "export function Calendar() {}"}

    def test_requirement_and_dependency_gaps_are_rejected(self):
        for change in ("missing_acceptance", "unknown_requirement", "forward_dependency"):
            plan = copy.deepcopy(example_plan())
            if change == "missing_acceptance":
                plan["requirements"][0]["acceptance"] = []
            elif change == "unknown_requirement":
                plan["tasks"][0]["requirement_ids"] = ["R99"]
            else:
                plan["tasks"][0]["depends_on"] = ["T2"]
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_plan(plan)

    def test_local_unit_result_is_diagnostic_and_cannot_complete_a_requirement(self):
        ledger = TaskLedger(self.root, example_plan(), 'Calendar')
        receipt = ledger.record('local_unit_check', 'vitest run Calendar.test.tsx', 0,
                                'local test passed', source_digest(self.files), ['R1'])
        self.assertEqual(ledger.evidence[-1]['requirement_ids'], [])
        with self.assertRaises(ValueError):
            ledger.update('T1', 'done', 'Local tests passed', [receipt])
        self.assertTrue(any('browser' in item for item in ledger.completion_issues(self.files)))

    def test_old_unit_shell_receipts_cannot_be_reused_as_acceptance(self):
        plan = example_plan()
        plan['requirements'][0]['verification'] = 'command'
        ledger = TaskLedger(self.root, plan, 'Calendar')
        receipt = ledger.record('run_shell', 'pytest tests/test_calendar.py', 0,
                                'passed', source_digest(self.files), ['R1'])
        self.assertEqual(ledger.evidence[-1]['kind'], 'local_unit_check')
        # Simulate a receipt created before local-unit evidence was separated.
        ledger.evidence[-1].update(kind='run_shell', requirement_ids=['R1'])
        with self.assertRaises(ValueError):
            ledger.update('T1', 'done', 'Old unit test passed', [receipt])
        self.assertTrue(any('command' in item for item in ledger.completion_issues(self.files)))

    def test_passing_build_does_not_complete_browser_requirement(self):
        ledger = TaskLedger(self.root, example_plan(), "Calendar")
        receipt = ledger.record("run_build", "npm run build", 0, "built", source_digest(self.files), ["R1"])
        ledger.update("T1", "done", "Code implemented", [receipt])
        issues = ledger.completion_issues(self.files)
        self.assertTrue(any("browser" in item for item in issues))
        browser_receipt = ledger.record("browser_check", "/", 0, "event creation and reload assertions passed", source_digest(self.files), ["R1"])
        ledger.update("T1", "done", "Flow verified", [browser_receipt])
        self.assertEqual(ledger.completion_issues(self.files), [])
        changed = {**self.files, "frontend/src/pages/Calendar.tsx": "broken code"}
        self.assertTrue(any("验收证据" in item for item in ledger.completion_issues(changed)))

    def test_normal_web_preview_does_not_require_api_receipt(self):
        plan = example_plan()
        plan['build_tier'] = 'normal'
        plan['architecture']['backend'] = {
            'required': True, 'reason': 'optional service adapter',
            'stack': 'FastAPI', 'directory': 'backend'
        }
        plan['requirements'].append({
            'id': 'R2', 'description': '服务端生成',
            'acceptance': ['请求接口返回结果'], 'verification': 'api'
        })
        plan['tasks'].append({
            'id': 'T2', 'title': '服务适配器', 'requirement_ids': ['R2'],
            'depends_on': ['T1'], 'files': ['backend/main.py'],
            'verification': '接口保留真实适配器'
        })
        files = dict(self.files)
        files['backend/main.py'] = 'from fastapi import FastAPI\napp = FastAPI()\n'
        ledger = TaskLedger(self.root, plan, 'Calendar with service adapter')
        issues = ledger.completion_issues(files)
        self.assertFalse(any('真实 HTTP 验证证据' in item for item in issues))
        self.assertFalse(any('缺少真实成功接口证据' in item for item in issues))

    def test_claimed_or_failed_verification_cannot_mark_task_done(self):
        ledger = TaskLedger(self.root, example_plan(), "Calendar")
        with self.assertRaises(ValueError):
            ledger.update("T1", "done", "looks good", [])
        receipt = ledger.record("browser_check", "/", 1, "button missing", source_digest(self.files), ["R1"])
        with self.assertRaises(ValueError):
            ledger.update("T1", "done", "ignore failure", [receipt])
        self.assertEqual(json.loads((self.root / '.atoms/task-state.json').read_text())["tasks"][0]["status"], "pending")

    def test_compaction_preserves_tool_call_result_pairs_and_tasks(self):
        ledger = TaskLedger(self.root, example_plan(), "Calendar")
        messages = [{"role": "system", "content": "instructions"}, {"role": "user", "content": "request"}]
        for index in range(8):
            messages += [{"role": "assistant", "content": "", "tool_calls": [{"id": str(index), "function": {"name": "read_file", "arguments": "{}"}}]},
                         {"role": "tool", "tool_call_id": str(index), "content": "x" * 1000}]
        compacted = compact_messages(messages, 2, ledger, [{"tool": "run_build", "result": "exit_code=0"}], max_chars=2000)
        self.assertLess(len(compacted), len(messages))
        self.assertIn("R1", compacted[2]["content"])
        self.assertIn("exit_code=0", compacted[2]["content"])
        calls = set()
        for message in compacted:
            if message["role"] == "assistant":
                calls.update(item["id"] for item in message.get("tool_calls", []))
            if message["role"] == "tool":
                self.assertIn(message["tool_call_id"], calls)

    def test_backend_sources_and_multi_test_commands_are_supported(self):
        (self.root / '.atoms-workspace.json').write_text(json.dumps({"test": ["npm run test", "python -m unittest"]}))
        self.assertEqual(configured_tests(self.root), ["npm run test", "python -m unittest"])
        source = review_sources({}, {"backend/app/routes/events.py": "def create_event(): pass", "frontend/src/api/events.ts": "export async function save() {}"})
        self.assertIn("backend/app/routes/events.py", source)
        self.assertIn("frontend/src/api/events.ts", source)


if __name__ == '__main__':
    unittest.main()
