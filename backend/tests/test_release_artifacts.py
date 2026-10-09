import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
import uuid

from release_artifacts import package_context, runtime_manifest
from published_database import credentials, names
from project_database import credentials as dev_credentials


class ReleaseArtifactTests(unittest.TestCase):
    def test_version_materialization_uses_history_and_never_changes_development(self):
        from release_versions import materialize_version
        from project_snapshots import save_blob
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'development'
            source.mkdir()
            asset = source / 'asset.bin'
            asset.write_bytes(b'old binary\x00')
            blob = save_blob(source, asset)
            asset.write_bytes(b'current binary\x00')
            destination = Path(folder) / 'historical-build'
            state = materialize_version(source, destination, {
                'files': {'app.py': 'VERSION=1', 'assets/asset.bin': blob, 'node_modules/stale.js': 'old dependency'},
                'runtime_state': {'dev_command': 'python app.py', 'modes': {'app.py': 0o755}}})
            self.assertEqual((destination / 'app.py').read_text(), 'VERSION=1')
            self.assertEqual((destination / 'assets/asset.bin').read_bytes(), b'old binary\x00')
            self.assertEqual((destination / 'app.py').stat().st_mode & 0o777, 0o755)
            self.assertFalse((destination / 'node_modules').exists())
            self.assertEqual(state['dev_command'], 'python app.py')
            self.assertEqual(asset.read_bytes(), b'current binary\x00')

    def test_missing_or_corrupt_history_never_falls_back_to_current_source(self):
        from release_versions import materialize_version
        from project_snapshots import save_blob, blob_path
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'development'
            source.mkdir()
            file = source / 'app.py'
            file.write_text('CURRENT=1')
            with self.assertRaisesRegex(ValueError, '源码快照'):
                materialize_version(source, Path(folder) / 'empty', {'files': {}})
            with self.assertRaises(ValueError):
                materialize_version(source, Path(folder) / 'escape', {'files': {'../app.py': 'bad'}})
            blob = save_blob(source, file)
            blob_path(source, blob).write_text('CORRUPT=1')
            with self.assertRaisesRegex(ValueError, '校验失败'):
                materialize_version(source, Path(folder) / 'corrupt', {'files': {'app.py': blob}})
            self.assertEqual(file.read_text(), 'CURRENT=1')

    def test_complete_archive_freezes_dependencies_and_excludes_development_state(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'project'
            root.mkdir()
            for name, content in {'dist/index.html': '<h1>v1</h1>', '.python-packages/provider.py': 'VERSION=1',
                                  'node_modules/library/index.js': 'v1', '.venv/pyvenv.cfg': 'private',
                                  'env.connector': 'ADMIN_PASSWORD=private', '.atoms-data/application-secrets.json': 'private',
                                  '.atoms/.agent-session/session.sqlite3': 'history', 'app.py': 'v1'}.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            # A project beyond the former 25 MB cap is still a normal artifact.
            large = root / 'dist/large.bin'
            with large.open('wb') as file:
                file.truncate(26 * 1024 * 1024)
            manifest = runtime_manifest(root, uuid.uuid4(), uuid.uuid4())
            output = Path(folder) / 'context.tar'
            result = package_context(root, output, manifest, 'sha256:base', os.getuid())
            (root / 'app.py').write_text('v2')
            with tarfile.open(output) as archive:
                paths = archive.getnames()
                self.assertIn('workspace/.python-packages/provider.py', paths)
                self.assertIn('workspace/node_modules/library/index.js', paths)
                self.assertIn('workspace/.venv/pyvenv.cfg', paths)
                self.assertEqual(archive.extractfile('workspace/app.py').read(), b'v1')
                self.assertNotIn('workspace/env.connector', paths)
                self.assertFalse(any('/.atoms-data/' in name or '/.atoms/' in name for name in paths))
            self.assertGreater(result['bytes'], 25 * 1024 * 1024)
            self.assertEqual(len(result['context_sha256']), 64)

    def test_host_escape_links_rejected_but_private_venv_links_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'project'
            root.mkdir()
            (root / 'bad').symlink_to('/etc/shadow')
            manifest = runtime_manifest(root, uuid.uuid4(), uuid.uuid4())
            with self.assertRaisesRegex(ValueError, '越界'):
                package_context(root, Path(folder) / 'bad.tar', manifest, 'sha256:base', os.getuid())
            (root / 'bad').unlink()
            (root / 'python').symlink_to('/usr/local/bin/python3')
            (root / 'python-relative').symlink_to('python')
            package_context(root, Path(folder) / 'good.tar', manifest, 'sha256:base', os.getuid())

    def test_release_dns_name_fits_one_label_and_retains_complete_identity(self):
        from project_publication import release_name
        project, release = uuid.uuid4(), uuid.uuid4()
        name = release_name(project, release)
        self.assertLessEqual(len(name), 63)
        self.assertIn(release.hex, name)
        self.assertNotEqual(name, release_name(project, uuid.uuid4()))

    def test_static_frontend_does_not_run_vite_and_backend_contract_is_retained(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'package.json').write_text(json.dumps({'scripts': {'dev': 'vite'}}))
            manifest = runtime_manifest(root, uuid.uuid4(), uuid.uuid4(), 'npm run dev')
            self.assertFalse(manifest['command'])
            settings = {'services': [{'name': 'api', 'command': 'python -m api', 'port_env': 'API_PORT', 'ready_path': '/api/health'}]}
            (root / '.atoms-workspace.json').write_text(json.dumps(settings))
            manifest = runtime_manifest(root, uuid.uuid4(), uuid.uuid4(), 'npm run dev')
            self.assertEqual(manifest['api_service'], 'api')
            self.assertEqual(manifest['services'], settings['services'])
            settings['publish'] = {'command': 'node production.js'}
            (root / '.atoms-workspace.json').write_text(json.dumps(settings))
            self.assertTrue(runtime_manifest(root, uuid.uuid4(), uuid.uuid4())['dynamic'])

    def test_vite_workspace_wrapper_still_uses_built_static_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'frontend').mkdir()
            (root / 'package.json').write_text(json.dumps({'scripts': {'dev': 'npm --prefix frontend run dev --'}}))
            (root / 'frontend/package.json').write_text(json.dumps({'devDependencies': {'vite': '6.4.3'}}))
            manifest = runtime_manifest(root, uuid.uuid4(), uuid.uuid4(), 'npm run dev -- --port $PORT')
            self.assertFalse(manifest['command'])
            self.assertFalse(manifest['dynamic'])

    def test_production_connector_preserves_contract_with_distinct_stable_identity(self):
        project = uuid.uuid4()
        url, secret = 'postgresql://admin:platform@db:5432/example', 'control-secret'
        prod = credentials(project, url, secret)
        dev = dev_credentials(project, url, secret)
        self.assertNotEqual(prod[0], dev[0])
        self.assertNotEqual(prod[1], dev[1])
        self.assertNotEqual(prod[2], dev[2])
        self.assertEqual(prod, credentials(project, url, secret))
        self.assertRegex(prod[0], r'^project_[0-9a-f]{32}$')
        self.assertEqual(prod[1], 'atoms_app_' + prod[0].removeprefix('project_'))
        self.assertNotEqual(names(project), names(uuid.uuid4()))
