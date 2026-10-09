"""Large-repository flows use a fake GitHub transport; nothing is published."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch
import zipfile

import httpx
import github_connector as github


class GitHubSizeTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_over_200_files_and_25mb_is_batched_with_one_final_ref_update(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(205):
                (root / f'{index:03}.txt').write_text('source')
            (root / 'large.bin').write_bytes(b'\0' * (26 * 1024 * 1024))
            (root / '.atoms-snapshots').mkdir()
            (root / '.atoms-snapshots/private').write_text('NEVER_EXPORT')
            (root / '.env').write_text('NEVER_EXPORT')
            project = uuid.uuid4()
            db = MagicMock()
            db.execute.return_value.fetchone.side_effect = [
                {'id': project, 'status': 'ready', 'title': 'large'},
                {'full_name': 'test/repo', 'default_branch': 'main'}]
            state = {'blobs': 0, 'trees': [], 'updates': 0}
            def handler(request):
                if request.method == 'GET' and '/git/ref/' in request.url.path:
                    return httpx.Response(200, json={'object': {'sha': 'parent'}})
                if request.method == 'GET':
                    return httpx.Response(200, json={'tree': {'sha': 'base'}})
                if request.url.path.endswith('/blobs'):
                    state['blobs'] += 1
                    return httpx.Response(201, json={'sha': f'blob-{state["blobs"]}'})
                data = json.loads(request.content)
                if request.url.path.endswith('/trees'):
                    state['trees'].append(data)
                    return httpx.Response(201, json={'sha': f'tree-{len(state["trees"])}'})
                if request.url.path.endswith('/commits'):
                    self.assertEqual(data['tree'], 'tree-3')
                    return httpx.Response(201, json={'sha': 'commit', 'html_url': 'https://example.invalid/commit'})
                state['updates'] += 1
                return httpx.Response(200, json={})
            real_client = httpx.AsyncClient
            def client(**kwargs):
                return real_client(transport=httpx.MockTransport(handler), **kwargs)
            with patch.object(github, 'connection') as connection, patch.object(github, 'access_token', AsyncMock(return_value='test-token')), patch('agent.project_root', return_value=root), patch.object(github.httpx, 'AsyncClient', side_effect=client):
                connection.return_value.__enter__.return_value = db
                result = await github.push_project(project, {'id': uuid.uuid4()})
            self.assertEqual(result['files'], 206)
            self.assertEqual(state['blobs'], 206)
            self.assertEqual([len(tree['tree']) for tree in state['trees']], [100, 100, 6])
            self.assertEqual([tree['base_tree'] for tree in state['trees']], ['base', 'tree-1', 'tree-2'])
            self.assertEqual(state['updates'], 1)

    async def test_import_over_old_expansion_file_count_and_header_limits_streams_to_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            archive_file = Path(directory) / 'repo.zip'
            with zipfile.ZipFile(archive_file, 'w', zipfile.ZIP_DEFLATED) as archive:
                with archive.open('repo/large.bin', 'w', force_zip64=True) as output:
                    for _ in range(201):
                        output.write(b'\0' * (1024 * 1024))
                for index in range(10001):
                    archive.writestr(f'repo/src/{index}.txt', 'source')
                archive.writestr('repo/.atoms-snapshots/private', 'NEVER_IMPORT')
            class ArchiveStream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    with archive_file.open('rb') as source:
                        while chunk := source.read(65536):
                            yield chunk
            def handler(request):
                if request.url.path.endswith('/zipball/main'):
                    return httpx.Response(200, stream=ArchiveStream(), headers={'content-length': str(60 * 1024 * 1024)})
                return httpx.Response(200, json={'id': 1, 'default_branch': 'main'})
            real_client = httpx.AsyncClient
            def client(**kwargs):
                return real_client(transport=httpx.MockTransport(handler), **kwargs)
            def workspace(project, *args, **kwargs):
                target = Path(directory) / 'projects' / project.hex
                target.mkdir(parents=True)
                return target
            user = {'id': uuid.uuid4()}
            data = github.ImportRequest(repository_id=1, full_name='test/repo', default_branch='main')
            with patch.dict(os.environ, {'WORKSPACE_ROOT': directory}), patch.object(github, 'connection'), patch.object(github, 'access_token', AsyncMock(return_value='test-token')), patch.object(github.httpx, 'AsyncClient', side_effect=client), patch('agent.ensure_workspace', side_effect=workspace), patch('agent.set_workspace_owner'), patch('main.grant_project_role'):
                result = await github.import_repository(data, user)
            root = Path(directory) / 'projects' / uuid.UUID(result['project_id']).hex
            self.assertEqual((root / 'large.bin').stat().st_size, 201 * 1024 * 1024)
            self.assertEqual(result['imported_files'], 10002)
            self.assertFalse((root / '.atoms-snapshots').exists())
            self.assertFalse(list(Path(directory).glob('.github-import-*')))
