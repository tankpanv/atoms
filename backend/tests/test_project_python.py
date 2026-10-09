"""Real pip/interpreter/runtime isolation tests; no PyPI or model calls."""
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch
import zipfile

import agent
import project_templates
import runtime
from project_python import environment


def wheel(folder, version):
    name = f'httpx-{version}'
    target = folder / f'{name}-py3-none-any.whl'
    files = {
        'httpx/__init__.py': f'__version__ = {version!r}\ndef main(): print(__version__)\n',
        f'{name}.dist-info/METADATA': f'Metadata-Version: 2.1\nName: httpx\nVersion: {version}\n',
        f'{name}.dist-info/WHEEL': 'Wheel-Version: 1.0\nGenerator: isolation-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n',
        f'{name}.dist-info/entry_points.txt': '[console_scripts]\natoms-python-probe = httpx:main\n',
    }
    files[f'{name}.dist-info/RECORD'] = ''.join(f'{path},,\n' for path in files)
    with zipfile.ZipFile(target, 'w') as archive:
        for path, content in files.items():
            archive.writestr(path, content)


class ProjectPythonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        Path(self.temporary.name).chmod(0o711)
        self.roots = {}
        for _ in range(2):
            project = uuid.uuid4()
            root = Path(self.temporary.name) / project.hex
            root.mkdir()
            (root / 'wheels').mkdir()
            for version in ('1.0.0', '2.0.0'):
                wheel(root / 'wheels', version)
            self.roots[project] = root
        self.projects = list(self.roots)
        self.patches = [patch('agent.ensure_workspace', side_effect=self.roots.__getitem__),
                        patch('runtime.ensure_workspace', side_effect=self.roots.__getitem__),
                        patch('project_database.environment', return_value={})]
        if os.geteuid() != 0:
            self.patches.extend([patch('agent.project_uid', return_value=os.getuid()),
                                 patch('runtime.project_uid', return_value=os.getuid())])
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        for project in self.projects:
            await runtime.stop_runtime(project)
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    def requirements(self, project, version):
        (self.roots[project] / 'requirements.txt').write_text(
            f'--no-index\n--find-links ./wheels\nhttpx=={version}\n')

    async def shell(self, project, command):
        code, output = await agent.run_shell(project, command)
        self.assertEqual(code, 0, output)
        return output.strip()

    async def test_repairs_legacy_ownership_and_excludes_platform_user_packages(self):
        project = self.projects[0]
        root = self.roots[project]
        legacy = root / '.python-packages/nested'
        legacy.mkdir(parents=True)
        (legacy / 'probe').write_text('legacy')
        (root / '.local/lib').mkdir(parents=True)
        output = await self.shell(project, "python -c 'import sys,site,importlib.util,json; print(json.dumps([sys.prefix,site.ENABLE_USER_SITE,importlib.util.find_spec(\"fastapi\") is None,sys.path]))'")
        prefix, user_site, missing_platform, paths = json.loads(output)
        self.assertEqual(prefix, str(root / '.venv'))
        self.assertFalse(user_site)
        self.assertTrue(missing_platform)
        self.assertFalse(any(path.startswith('/usr/local/lib/') and 'site-packages' in path for path in paths))
        self.assertEqual((legacy / 'probe').stat().st_uid, agent.project_uid(project))
        await self.shell(project, "python -c 'from pathlib import Path; Path(\".python-packages/nested/probe\").write_text(\"writable\")'")
        self.assertFalse(any(name.startswith(('.venv/', '.python-cache/', '.python-packages/')) for name in agent.snapshot_files(root)))

    async def test_real_pip_versions_are_private_and_commands_use_project_interpreter(self):
        first, second = self.projects
        for project, version in ((first, '1.0.0'), (second, '2.0.0')):
            self.requirements(project, version)
            code, output = await agent.ensure_python_dependencies(project)
            self.assertEqual(code, 0, output)
            self.assertEqual(await self.shell(project, 'atoms-python-probe'), version)
            self.assertEqual(await self.shell(project, "python -c 'import httpx; print(httpx.__version__)'"), version)
            await self.shell(project, "pip --version; python3 -m pip --version")
        (self.roots[first] / '.python-packages/httpx/__init__.py').write_text('__version__="changed-in-A"\n')
        self.assertEqual(await self.shell(second, "python -c 'import httpx; print(httpx.__version__)'"), '2.0.0')
        import httpx
        self.assertNotIn(httpx.__version__, ('1.0.0', '2.0.0', 'changed-in-A'))

    async def test_changed_manifest_removes_stale_packages_and_failed_install_is_retryable(self):
        project = self.projects[0]
        root = self.roots[project]
        self.requirements(project, '1.0.0')
        self.assertEqual((await agent.ensure_python_dependencies(project))[0], 0)
        state = (root / '.python-cache/dependency-state.json').read_text()
        (root / '.python-packages/removed_dependency.py').write_text('old=True\n')
        self.requirements(project, '9.0.0')
        code, output = await agent.ensure_python_dependencies(project)
        self.assertNotEqual(code, 0, output)
        self.assertEqual((root / '.python-cache/dependency-state.json').read_text(), state)
        self.assertEqual(await self.shell(project, 'atoms-python-probe'), '1.0.0')
        self.requirements(project, '2.0.0')
        self.assertEqual((await agent.ensure_python_dependencies(project))[0], 0)
        self.assertEqual(await self.shell(project, 'atoms-python-probe'), '2.0.0')
        self.assertFalse((root / '.python-packages/removed_dependency.py').exists())
        self.assertFalse((root / '.python-packages/httpx-1.0.0.dist-info').exists())
        with patch('agent.run_command', side_effect=AssertionError('unchanged requirements must reuse install')):
            self.assertEqual((await agent.ensure_python_dependencies(project))[0], 0)

    async def test_runtime_installs_requirements_without_prior_build(self):
        project = self.projects[0]
        root = self.roots[project]
        self.requirements(project, '2.0.0')
        (root / 'server.py').write_text('''import http.server,json,os,sys,httpx
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(json.dumps({'version':httpx.__version__,'prefix':sys.prefix}).encode())
http.server.HTTPServer(('127.0.0.1',int(os.environ['PORT'])),Handler).serve_forever()
''')
        (root / '.atoms-workspace.json').write_text(json.dumps({'dev': 'python server.py'}))
        started = await runtime.start_runtime(project)
        import httpx
        async with httpx.AsyncClient() as client:
            response = await client.get(f'http://127.0.0.1:{started.port}/')
        self.assertEqual(response.json(), {'version': '2.0.0', 'prefix': str(root / '.venv')})

    async def test_python_only_build_and_cache_copy_into_precreated_directory(self):
        project = self.projects[0]
        root = self.roots[project]
        self.requirements(project, '1.0.0')
        code, output = await agent.run_build(project)
        self.assertEqual(code, 0, output)
        cache = Path(self.temporary.name) / 'cache'
        (cache / '.python-packages').mkdir(parents=True)
        (cache / '.python-packages/from_cache.py').write_text('value=42\n')
        (cache / 'fingerprint').write_text(project_templates.python_dependency_fingerprint(root))
        destination = root / '.python-cache/copy-test'
        destination.mkdir()
        with patch.object(project_templates, 'PYTHON_CACHE_ROOT', cache):
            self.assertTrue(project_templates.restore_python_dependency_cache(root, agent.project_uid(project), destination))
        self.assertEqual((destination / 'from_cache.py').stat().st_uid, agent.project_uid(project))

    def test_environment_rejects_linked_dependency_directory(self):
        project = self.projects[0]
        root = self.roots[project]
        (root / '.python-packages').symlink_to(self.roots[self.projects[1]], target_is_directory=True)
        with self.assertRaisesRegex(ValueError, '符号链接'):
            environment(root, agent.project_uid(project))

    @unittest.skipUnless((project_templates.PYTHON_CACHE_ROOT / 'fingerprint').exists(), 'requires worker image cache')
    async def test_cached_native_extensions_load_from_workspace_under_project_uid(self):
        project = self.projects[0]
        root = self.roots[project]
        (root / 'requirements.txt').write_bytes((project_templates.PYTHON_CACHE_ROOT / 'requirements.txt').read_bytes())
        code, output = await agent.ensure_python_dependencies(project)
        self.assertEqual(code, 0, output)
        self.assertIn('预装', output)
        await self.shell(project, "python -c 'import fastapi,psycopg,pydantic_core._pydantic_core; print(fastapi.__version__)'")
