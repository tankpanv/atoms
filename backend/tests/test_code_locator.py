import json
import os
import tempfile
import unittest
from pathlib import Path

from agent import snapshot_files
from agent_session import AgentSession, inventory
from code_locator import CodeLocator, matches, validate_change_map
from tool_contract import parse_tool_arguments, ToolArgumentError
from code_locator import DISCOVERY_TOOLS


class CodeLocatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.write('backend/db.py', 'def connect():\n    return "db"\n')
        self.write('backend/routes.py', 'from .db import connect\n\n@router.post("/register")\ndef register(payload):\n    return connect()\n')
        self.write('frontend/src/api.ts', 'export const register = () => fetch("/register")\n')
        self.write('frontend/src/pages/Login.tsx', 'import { register } from "@/api"\nexport function Login() { return register() }\n')
        self.write('.env', 'NEVER_SEND_PRIVATE_VALUE=hidden')
        self.session = AgentSession(self.root)
        self.locator = CodeLocator(self.root, self.session)
        self.manifest = self.locator.sync()

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def test_warm_index_reads_zero_unchanged_bodies_and_invalidates_same_size_edit(self):
        self.locator.sync()
        self.assertEqual(self.locator.stats['bytes_read'], 0)
        target = self.root / 'backend/db.py'
        before = target.stat()
        self.write('backend/db.py', 'def connect():\n    return "DB"\n')
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        current = self.locator.sync()
        self.assertEqual(self.locator.stats['reindexed_files'], 1)
        self.assertNotEqual(inventory(self.manifest)['backend/db.py'], inventory(current)['backend/db.py'])
        target.unlink()
        self.locator.sync()
        self.assertNotIn('backend/db.py', self.locator.entries)
        self.assertEqual(self.locator.dependencies['backend/routes.py'], [])

    def test_manifest_hashes_match_real_snapshots_including_binary_and_secret_exclusion(self):
        (self.root / 'image.png').write_bytes(b'\x89PNG\x00\xff')
        (self.root / 'windows.py').write_bytes(b'print("hello")\r\nprint("next")\r')
        manifest = self.locator.sync()
        self.assertEqual(inventory(manifest), inventory(snapshot_files(self.root)))
        rendered, _, _ = self.locator.read({'path': 'windows.py'}, [])
        self.assertIn('代码行 1–2', rendered)
        self.assertNotIn('.env', manifest)
        self.assertNotIn('NEVER_SEND_PRIVATE_VALUE', self.locator.overview())

    def test_python_package_roots_and_relative_aliases_use_existing_unambiguous_targets(self):
        self.write('backend/app/service.py', 'from app.database import session\nfrom . import models\n')
        self.write('backend/app/database.py', 'session = None\n')
        self.write('backend/app/models.py', 'class User: pass\n')
        self.locator.sync()
        self.assertEqual(self.locator.dependencies['backend/app/service.py'], ['backend/app/database.py', 'backend/app/models.py'])
        self.write('other/app/database.py', 'session = None\n')
        self.locator.sync()
        self.assertEqual(self.locator.dependencies['backend/app/service.py'], ['backend/app/models.py'])

    def test_demand_queries_return_current_routes_symbols_and_real_importers(self):
        result = json.loads(self.locator.locate(['register']))
        route = next(row for row in result['candidates'] if row['path'] == 'backend/routes.py')
        self.assertEqual(route['routes'][0]['path'], '/register')
        self.assertEqual(route['imports'], ['backend/db.py'])
        api = next(row for row in result['candidates'] if row['path'] == 'frontend/src/api.ts')
        self.assertEqual(api['imported_by'], ['frontend/src/pages/Login.tsx'])
        self.assertEqual(json.loads(self.locator.locate(['totally-missing-capability']))['matches'], 0)
        self.assertNotIn('return connect()', self.locator.overview())

    def test_glob_has_directory_boundaries_and_zero_or_multiple_recursive_segments(self):
        for path in ('frontend/src/a.ts', 'frontend/src/pages/a.ts', 'frontend/src/pages/deep/a.ts'):
            self.assertTrue(matches(path, 'frontend/src/**/*.ts'))
        self.assertFalse(matches('frontend/src/pages/a.ts', 'frontend/src/*.ts'))
        self.assertTrue(matches('a/b/c/d.ts', '**/b/**/d.ts'))
        self.assertEqual(json.loads(self.locator.glob('frontend/src/**/*.ts'))['paths'], ['frontend/src/api.ts'])

    def test_grep_returns_scoped_real_lines_and_pagination_without_full_bodies(self):
        self.write('frontend/src/api.ts', 'export const register = () => fetch("/register")\n// register next\n')
        self.locator.sync()
        result = json.loads(self.locator.grep('register', 'frontend/src/*.ts', limit=1))
        self.assertEqual(result['total_matches'], 2)
        self.assertEqual(result['next_offset'], 1)
        self.assertEqual(result['matches'][0]['line'], 1)
        self.assertEqual(json.loads(self.locator.grep('register', 'frontend/src/*.ts', offset=1))['matches'][0]['line'], 2)
        with self.assertRaises(ValueError):
            self.locator.grep('[invalid', regex=True)

    def test_symbol_read_uses_actual_offsets_and_tracks_source_version(self):
        rendered, text, offset = self.locator.read({'path': 'backend/routes.py', 'symbol': 'register', 'max_lines': 2}, [])
        self.assertEqual(offset, text.index('def register'))
        self.assertIn('代码行 4–5', rendered)
        self.assertIn('return connect()', rendered)
        self.assertNotIn('from .db', rendered)
        self.assertIn(self.locator.entries['backend/routes.py']['sha'][:16], rendered)
        with self.assertRaises(ValueError):
            self.locator.read({'path': '.env'}, [])

    def test_navigation_page_hints_are_capped_but_invalid_ranges_remain_errors(self):
        for name, args, field, cap in [('read_code', {'path': 'backend/routes.py', 'max_lines': 1000}, 'max_lines', 160),
                                      ('search_code', {'query': 'register', 'limit': 1000}, 'limit', 80)]:
            adjustments = []
            parsed = parse_tool_arguments({'function': {'name': name, 'arguments': json.dumps(args)}}, DISCOVERY_TOOLS, adjustments=adjustments)
            self.assertEqual(parsed[field], cap)
            self.assertEqual(adjustments[0]['effective'], cap)
        with self.assertRaises(ToolArgumentError):
            parse_tool_arguments({'function': {'name': 'read_code', 'arguments': json.dumps({'path': 'backend/routes.py', 'start_line': 0})}}, DISCOVERY_TOOLS)

    def plan(self):
        return {'requirements': [{'id': 'R1'}], 'change_map': [{'path': 'backend/routes.py', 'operation': 'modify',
                'reason': 'Add requested registration behavior', 'requirement_ids': ['R1']}]}

    def test_change_map_requires_real_sites_and_complete_current_requirement_coverage(self):
        plan = validate_change_map(self.plan(), self.locator)
        self.assertEqual(plan['change_map'][0]['source_sha'], self.locator.entries['backend/routes.py']['sha'])
        for path, operation in [('missing.py', 'modify'), ('backend/routes.py', 'create'), ('backend/./routes.py', 'create'), ('../outside.py', 'create'), ('backend', 'create'), ('.env', 'modify')]:
            invalid = self.plan()
            invalid['change_map'][0].update(path=path, operation=operation)
            with self.assertRaises(ValueError, msg=path):
                validate_change_map(invalid, self.locator)
        missing = self.plan()
        missing['requirements'].append({'id': 'R2'})
        with self.assertRaisesRegex(ValueError, 'R2'):
            validate_change_map(missing, self.locator)
        divergent = self.plan()
        divergent['tasks'] = [{'id': 'T1', 'files': ['backend/db.py']}]
        with self.assertRaisesRegex(ValueError, 'backend/db.py'):
            validate_change_map(divergent, self.locator)

    def test_handoff_rechecks_changed_sites_and_does_not_claim_old_observations_are_current(self):
        plan = validate_change_map(self.plan(), self.locator)
        self.session.save_phase('planning', {'request': 'new feature', 'completed': True, 'plan': plan, 'plan_hash': __import__('agent_session').digest(plan), 'observations': {}})
        self.write('backend/routes.py', '# concurrent new implementation\n')
        handoff = self.session.planning_handoff('new feature', plan, self.locator.sync())
        self.assertIn('backend/routes.py', handoff)
        self.assertIn('重新读取', handoff)
        created = {'path': 'new.py', 'operation': 'create', 'reason': 'new module', 'requirement_ids': ['R1']}
        plan['change_map'] = [created]
        self.session.save_phase('planning', {'request': 'new feature', 'completed': True, 'plan_hash': __import__('agent_session').digest(plan), 'observations': {}})
        self.write('new.py', 'existing = True\n')
        self.assertIn('new.py', self.session.planning_handoff('new feature', plan, self.locator.sync()).split('重新读取：')[-1])
