import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import AsyncMock, Mock, patch
import zipfile

import agent
from agent_harness import source_digest
from agent_session import AgentSession, inventory
from code_locator import CodeLocator
from coding_runtime import changed_diff
from project_artifacts import artifacts, artifact_file
from project_clone import copy_source
from project_snapshots import archive_response, receive_upload, blob_path, DIRECTORY
from verification_policy import inspect_sources


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / uuid.uuid4().hex
        self.root.mkdir()
        self.owner = patch('agent.project_uid', return_value=os.getuid())
        self.owner.start()
        self.addCleanup(self.owner.stop)

    def large_asset(self, megabytes=32):
        target = self.root / 'public/media.bin'
        target.parent.mkdir()
        with target.open('wb') as stream:
            for _ in range(megabytes):
                stream.write(b'\0\xff' * (512 * 1024))
        return target

    def test_over_25mb_snapshots_restore_and_index_with_bounded_reads(self):
        asset = self.large_asset()
        (self.root / 'app.py').write_text('print("business")\n')
        original = Path.read_bytes
        def bounded(path):
            if path.stat().st_size > 120000:
                raise AssertionError('large assets must stream instead of reading whole bodies')
            return original(path)
        with patch.object(Path, 'read_bytes', bounded):
            files = agent.snapshot_files(self.root)
            locator = CodeLocator(self.root, AgentSession(self.root))
            indexed = locator.sync()
        self.assertLess(len(json.dumps(files)), 5000)
        self.assertEqual(inventory(files), inventory(indexed))
        self.assertNotIn(DIRECTORY, ' '.join(agent.list_files(self.root)))
        (self.root / 'app.py').write_text('changed\n')
        asset.write_bytes(b'replaced')
        (self.root / 'extra.txt').write_text('remove on restore')
        agent.restore_files(self.root, json.loads(json.dumps(files)))
        self.assertEqual(asset.stat().st_size, 32 * 1024 * 1024)
        self.assertEqual((self.root / 'app.py').read_text(), 'print("business")\n')
        self.assertFalse((self.root / 'extra.txt').exists())
        self.assertEqual(source_digest(files), source_digest(agent.snapshot_files(self.root)))

    def test_warm_large_snapshots_reuse_objects_and_detect_same_size_edits(self):
        asset = self.large_asset(1)
        before = agent.snapshot_files(self.root)
        with patch('project_snapshots.save_blob', side_effect=AssertionError('unchanged asset copied again')):
            self.assertEqual(before, agent.snapshot_files(self.root))
        stat = asset.stat()
        with asset.open('r+b') as stream:
            stream.write(b'NEW')
        os.utime(asset, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        after = agent.snapshot_files(self.root)
        self.assertNotEqual(inventory(before), inventory(after))
        agent.restore_files(self.root, before)
        self.assertEqual(source_digest(before), source_digest(agent.snapshot_files(self.root)))

    def test_many_small_files_have_small_metadata_and_storage_layout_does_not_change_source_identity(self):
        for index in range(300):
            (self.root / f'{index:03}.txt').write_text('文' * 33000)
        first = agent.snapshot_files(self.root)
        self.assertLess(len(json.dumps(first).encode()), 3 * 1024 * 1024)
        with patch('project_snapshots.INLINE_TOTAL_BYTES', 0):
            external = agent.snapshot_files(self.root)
        self.assertEqual(inventory(first), inventory(external))
        self.assertEqual(source_digest(first), source_digest(external))
        self.assertNotIn('.txt', changed_diff(first, external))
        agent.restore_files(self.root, first)
        self.assertEqual((self.root / '299.txt').read_text(), '文' * 33000)

    def test_missing_or_corrupt_objects_do_not_delete_current_files(self):
        self.large_asset(1)
        files = agent.snapshot_files(self.root)
        current = self.root / 'current.txt'
        current.write_text('keep me')
        stored = blob_path(self.root, files['public/media.bin'])
        saved = stored.read_bytes()
        stored.write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, '校验失败'):
            agent.restore_files(self.root, files)
        self.assertEqual(current.read_text(), 'keep me')
        stored.write_bytes(saved)
        stored.unlink()
        with self.assertRaisesRegex(ValueError, '缺失'):
            agent.restore_files(self.root, files)
        self.assertEqual(current.read_text(), 'keep me')

    def test_legacy_inline_and_base64_versions_still_restore(self):
        agent.restore_files(self.root, {'old.py': 'print("legacy")\n', 'old.png':
                            {'encoding': 'base64', 'content': base64.b64encode(b'\x00\xff').decode()}})
        self.assertEqual((self.root / 'old.png').read_bytes(), b'\x00\xff')
        expected = hashlib.sha256(b'\x00\xff').hexdigest()
        self.assertEqual(inventory(agent.snapshot_files(self.root))['old.png'], expected)

    def test_restore_version_endpoint_accepts_and_prevalidates_blob_versions(self):
        from jobs import restore_version
        asset = self.large_asset(1)
        files = agent.snapshot_files(self.root)
        asset.write_bytes(b'changed')
        def execute(query, params=None):
            row = ({'n': 2} if 'MAX(version)' in query else None if 'SELECT result FROM project_restores' in query else {'status': 'error', 'dev_command': ''} if 'SELECT status,dev_command' in query else {'files': files, 'preview_html': '', 'runtime_state': {}})
            return Mock(fetchone=Mock(return_value=row))
        with patch('jobs.connection') as connection, \
             patch('agent.ensure_workspace', return_value=self.root), \
             patch('agent.run_build', new=AsyncMock(return_value=(0, 'built'))), \
             patch('agent.preview_document', return_value='preview'), \
             patch('runtime.start_runtime', new=AsyncMock(return_value=None)), \
             patch('agent_session.session_status', return_value={'application_type': 'artifact'}), \
             patch('runtime.stop_runtime', new=AsyncMock()) as stop:
            connection.return_value.__enter__.return_value.execute.side_effect = execute
            self.assertEqual(asyncio.run(restore_version(uuid.UUID(self.root.name), 1)), 2)
            self.assertEqual(asset.stat().st_size, 1024 * 1024)
            stop.assert_awaited_once()
            stop.reset_mock()
            blob_path(self.root, files['public/media.bin']).write_bytes(b'corrupt')
            asset.write_bytes(b'current stays')
            with self.assertRaisesRegex(ValueError, '校验失败'):
                asyncio.run(restore_version(uuid.UUID(self.root.name), 1))
            stop.assert_not_awaited()
            self.assertEqual(asset.read_bytes(), b'current stays')

    def test_restore_handles_file_directory_refactors_without_deleting_private_files(self):
        (self.root / 'module').write_text('old file')
        agent.restore_files(self.root, {'module/main.py': 'print("nested")\n'})
        self.assertTrue((self.root / 'module/main.py').is_file())
        agent.restore_files(self.root, {'module': 'file again'})
        self.assertEqual((self.root / 'module').read_text(), 'file again')
        agent.restore_files(self.root, {'module/main.py': 'print("nested")\n'})
        (self.root / 'module/.env').write_text('keep private')
        with self.assertRaisesRegex(ValueError, '未纳入版本'):
            agent.restore_files(self.root, {'module': 'file again'})
        self.assertEqual((self.root / 'module/.env').read_text(), 'keep private')
        self.assertTrue((self.root / 'module/main.py').exists())

    def test_large_text_editing_paging_and_syntax_checks(self):
        content = '# 中文源码\n' * 30000 + 'value = 1\n'
        agent.write_file(self.root, 'large.py', content)
        self.assertEqual(agent.read_file(self.root, 'large.py'), content)
        session = AgentSession(self.root)
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('paged read loaded whole large text')):
            rendered, sha = agent.session_read_file(session, self.root, 'large.py', len(content)-10, 100, [])
        self.assertIn('value = 1', rendered)
        self.assertEqual(sha, hashlib.sha256(content.encode()).hexdigest())
        self.assertLess(len(rendered), 500)
        files = agent.snapshot_files(self.root)
        self.assertEqual(inspect_sources(files)[0], 0)
        agent.replace_in_file(self.root, 'large.py', 'value = 1', 'value = 2')
        self.assertTrue(agent.read_file(self.root, 'large.py').endswith('value = 2\n'))
        (self.root / 'large.py').write_text(content + 'def broken(:\n')
        self.assertEqual(inspect_sources(agent.snapshot_files(self.root))[0], 1)

    def test_snapshot_store_and_object_paths_cannot_escape_project(self):
        self.large_asset(1)
        with tempfile.TemporaryDirectory() as outside:
            (self.root / DIRECTORY).symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                agent.snapshot_files(self.root)
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_clone_exceeds_old_byte_and_file_limits_and_excludes_dependency_workarounds(self):
        (self.root / 'large.bin').touch()
        with (self.root / 'large.bin').open('r+b') as stream:
            stream.truncate(257 * 1024 * 1024)
        source = self.root / 'many'
        source.mkdir()
        for index in range(10001):
            (source / str(index)).touch()
        for name in (DIRECTORY, '.runtime-python-deps', '.python-cache'):
            (self.root / name).mkdir()
            (self.root / name / 'excluded').write_text('cache')
        with tempfile.TemporaryDirectory() as target:
            result = copy_source(self.root, target)
            self.assertEqual(result['files'], 10002)
            self.assertGreater(result['bytes'], 256 * 1024 * 1024)
            self.assertFalse((Path(target) / DIRECTORY).exists())

    def test_all_artifacts_remain_downloadable_after_first_hundred(self):
        (self.root / 'dist').mkdir()
        for index in range(105):
            (self.root / 'dist' / f'{index:03}.txt').write_text('deliverable')
        self.assertEqual(len(artifacts(self.root)), 105)
        self.assertTrue(artifact_file(self.root, 'dist/104.txt').is_file())


class TransferTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_over_old_5mb_threshold_and_interruption_preserve_original(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'asset.bin'
            target.write_bytes(b'old')
            class Request:
                async def stream(self):
                    for _ in range(64):
                        self_test.assertEqual(target.read_bytes(), b'old')
                        yield b'x' * 100000
            self_test = self
            self.assertEqual(await receive_upload(Request(), target), 6400000)
            self.assertEqual(target.stat().st_size, 6400000)
            class Interrupted:
                async def stream(self):
                    yield b'partial'
                    raise asyncio.CancelledError()
            with self.assertRaises(asyncio.CancelledError):
                await receive_upload(Interrupted(), target)
            self.assertEqual(target.stat().st_size, 6400000)
            self.assertFalse(list(Path(folder).glob('.atoms-upload-*')))

    async def test_download_archive_uses_disk_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'large.txt'
            source.write_text('hello' * 1500000)
            response = archive_response(root, [(source, 'large.txt')], 'source.zip')
            with zipfile.ZipFile(response.path) as archive:
                self.assertEqual(archive.getinfo('large.txt').file_size, source.stat().st_size)
            await response.background()
            self.assertFalse(Path(response.path).exists())
