import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch
from project_cleanup import delete_project_directories, delete_project_storage
from published_storage import delete_objects


class ProjectCleanupTests(unittest.TestCase):
    def test_directories_and_caches_removed_without_touching_neighbour(self):
        project, owner = uuid.uuid4(), uuid.uuid4()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in [root/'projects'/project.hex, root/'users'/owner.hex/project.hex, root/project.hex]:
                (path/'.cache').mkdir(parents=True)
                (path/'.cache/context').write_text('cache')
            neighbour = root/'projects'/uuid.uuid4().hex
            neighbour.mkdir(); (neighbour/'code').write_text('preserve')
            for _ in range(2):  # retry after partial/successful cleanup is safe
                delete_project_directories(root, project, owner, root/'projects'/project.hex)
            self.assertEqual((neighbour/'code').read_text(), 'preserve')
            self.assertFalse((root/project.hex).exists())
            self.assertFalse((root/'users'/owner.hex/project.hex).exists())

    def test_parent_symlink_cannot_delete_outside_workspace(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root=Path(directory); project=uuid.uuid4()
            (root/'projects').symlink_to(outside, target_is_directory=True)
            target=Path(outside)/project.hex; target.mkdir(); (target/'code').write_text('preserve')
            with self.assertRaises(ValueError):
                delete_project_directories(root, project, uuid.uuid4(), root/'projects'/project.hex)
            self.assertTrue((target/'code').exists())

    def test_permission_failure_is_not_hidden(self):
        with tempfile.TemporaryDirectory() as directory:
            project=uuid.uuid4(); root=Path(directory); (root/project.hex).mkdir()
            with patch('project_cleanup.shutil.rmtree', side_effect=PermissionError('denied')):
                with self.assertRaises(PermissionError):
                    delete_project_directories(root, project, uuid.uuid4(), root/project.hex)

    def test_removes_prior_versions_delete_markers_and_incomplete_uploads(self):
        project=uuid.uuid4(); prefix=f'projects/{project}/'; s3=Mock()
        versions=Mock(); versions.paginate.return_value=[{'Versions':[{'Key':prefix+'old/index.html','VersionId':'v1'}], 'DeleteMarkers':[{'Key':prefix+'old/index.html','VersionId':'v2'}]}]
        uploads=Mock(); uploads.paginate.return_value=[{'Uploads':[{'Key':prefix+'incomplete/a.js','UploadId':'upload'}, {'Key':'projects/other-project/incomplete','UploadId':'foreign'}]}]
        s3.get_paginator.side_effect=lambda name: versions if name=='list_object_versions' else uploads
        s3.delete_objects.return_value={}
        with patch('project_cleanup.client', return_value=s3), patch('project_cleanup.BUCKET','bucket'):
            delete_project_storage(project, [{'bucket':'bucket','object_key':prefix+'new/index.html'}])
        versions.paginate.assert_called_once_with(Bucket='bucket',Prefix=prefix)
        uploads.paginate.assert_called_once_with(Bucket='bucket')
        self.assertEqual(len(s3.delete_objects.call_args.kwargs['Delete']['Objects']),2)
        s3.abort_multipart_upload.assert_called_once_with(Bucket='bucket', Key=prefix+'incomplete/a.js', UploadId='upload')

    def test_object_partial_failures_raise_and_foreign_keys_rejected(self):
        project=uuid.uuid4(); prefix=f'projects/{project}/'; s3=Mock()
        s3.get_paginator.return_value.paginate.return_value=[{'Versions':[{'Key':prefix+'a','VersionId':'v'}]}]
        s3.delete_objects.return_value={'Errors':[{'Code':'AccessDenied'}]}
        with patch('project_cleanup.client',return_value=s3):
            with self.assertRaises(RuntimeError): delete_project_storage(project, [])
            with self.assertRaises(ValueError): delete_project_storage(project, [{'bucket':'bucket','object_key':'other/project'}])
        with patch('published_storage.client',return_value=s3):
            with self.assertRaises(RuntimeError): delete_objects('bucket',['key'])
