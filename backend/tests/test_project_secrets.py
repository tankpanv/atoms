"""Local auth setup works without user credentials or weakened authentication."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

import agent
import runtime
from agent_checks import http_request
from project_clone import copy_source
from project_secrets import environment, LOCAL_SECRET_NAMES


class ProjectSecretsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_private_persistent_project_values_never_inherit_platform_credentials(self):
        with patch.dict(os.environ, {'AUTH_TOKEN_SECRET': 'platform-only', 'OPENAI_API_KEY': 'platform-api'}):
            first = environment(self.root, os.getuid())
            self.assertEqual(first, environment(self.root, os.getuid()))
        self.assertEqual(set(first), LOCAL_SECRET_NAMES)
        self.assertNotEqual(first['AUTH_TOKEN_SECRET'], 'platform-only')
        self.assertTrue(all(len(value) >= 32 for value in first.values()))
        private = self.root / '.atoms-data/application-secrets.json'
        self.assertEqual(private.stat().st_mode & 0o777, 0o600)
        self.assertEqual(private.stat().st_uid, os.getuid())
        self.assertEqual(agent.list_files(self.root), [])
        with tempfile.TemporaryDirectory() as clone_directory:
            clone = Path(clone_directory)
            copy_source(self.root, clone)
            self.assertFalse((clone / '.atoms-data').exists())
            self.assertNotEqual(environment(clone, os.getuid()), first)

    def test_concurrent_initialization_does_not_rotate_keys(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            values = list(pool.map(lambda _: environment(self.root, os.getuid()), range(12)))
        self.assertTrue(all(value == values[0] for value in values))
        self.assertFalse(list((self.root / '.atoms-data').glob('.application-secrets-*')))

    @unittest.skipUnless(os.getuid() == 0, 'requires worker root with CHOWN capability')
    def test_repeated_reads_of_project_uid_files_do_not_require_fowner_capability(self):
        project_uid = 100123
        first = environment(self.root, project_uid)
        self.assertEqual(first, environment(self.root, project_uid))
        path = self.root / '.atoms-data/application-secrets.json'
        self.assertEqual(path.stat().st_uid, project_uid)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_explicit_project_key_is_preserved_and_corruption_is_not_overwritten(self):
        environment(self.root, os.getuid())
        path = self.root / '.atoms-data/application-secrets.json'
        saved = json.loads(path.read_text())
        saved['AUTH_TOKEN_SECRET'] = 'an-existing-project-specific-secret-that-stays-stable'
        path.write_text(json.dumps(saved))
        self.assertEqual(environment(self.root, os.getuid())['AUTH_TOKEN_SECRET'], saved['AUTH_TOKEN_SECRET'])
        path.write_text('{broken')
        with self.assertRaisesRegex(ValueError, '不自动轮换'):
            environment(self.root, os.getuid())
        self.assertEqual(path.read_text(), '{broken')

    def test_symlinked_configuration_cannot_read_or_replace_outside_files(self):
        outside = self.root / 'outside'
        outside.mkdir()
        data = self.root / '.atoms-data'
        data.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            environment(self.root, os.getuid())
        data.unlink()
        data.mkdir()
        secret = outside / 'secret.json'
        secret.write_text('private outside data')
        (data / 'application-secrets.json').symlink_to(secret)
        with self.assertRaises(ValueError):
            environment(self.root, os.getuid())
        self.assertEqual(secret.read_text(), 'private outside data')


class ProjectSecretsRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_shell_and_http_service_share_persistent_keys_across_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            project = uuid.uuid4()
            root = Path(directory) / project.hex
            root.mkdir()
            (root / 'server.py').write_text('''import hashlib, http.server, json, os
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({'secret_hash': hashlib.sha256(os.environ['AUTH_TOKEN_SECRET'].encode()).hexdigest()}).encode()
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(body)))
        self.end_headers()
        self.wfile.write(body)
http.server.HTTPServer(('127.0.0.1',int(os.environ['PORT'])),Handler).serve_forever()
''')
            (root / '.atoms-workspace.json').write_text(json.dumps({'dev': 'python server.py'}))
            with patch('agent.ensure_workspace', return_value=root), \
                 patch('runtime.ensure_workspace', return_value=root), \
                 patch('agent.project_uid', return_value=os.getuid()), \
                 patch('runtime.project_uid', return_value=os.getuid()), \
                 patch('project_database.environment', return_value={}):
                try:
                    code, output = await agent.run_shell(project,
                        "python -c 'import hashlib,os; print(hashlib.sha256(os.environ[\"AUTH_TOKEN_SECRET\"].encode()).hexdigest())'")
                    self.assertEqual(code, 0, output)
                    expected = output.strip()
                    for _ in range(2):
                        code, report = await http_request(project, {'path': '/', 'expect_json': {'secret_hash': expected}})
                        self.assertEqual(code, 0, report)
                        await runtime.stop_runtime(project)
                finally:
                    await runtime.stop_runtime(project)
