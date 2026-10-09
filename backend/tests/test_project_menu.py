import tempfile
import unittest
from pathlib import Path
from project_clone import copy_source,unpack_docker_stream

class CloneBoundaryTests(unittest.TestCase):
    def test_source_copy_excludes_secrets_caches_and_symlinks(self):
        with tempfile.TemporaryDirectory() as a,tempfile.TemporaryDirectory() as b,tempfile.TemporaryDirectory() as outside:
            source,target=Path(a),Path(b)
            (source/'frontend').mkdir();(source/'frontend'/'app.ts').write_text('business code')
            (source/'.env').write_text('SECRET');(source/'env.connector').write_text('DSN');(source/'.npm-cache').mkdir();(source/'.npm-cache'/'token').write_text('SECRET')
            (source/'.atoms').mkdir();(source/'.atoms'/'checkpoint.json').write_text('old session')
            (Path(outside)/'private').write_text('private');(source/'external').symlink_to(outside,target_is_directory=True)
            result=copy_source(source,target)
            self.assertEqual(result['files'],1);self.assertEqual((target/'frontend'/'app.ts').read_text(),'business code')
            self.assertFalse((target/'.env').exists());self.assertFalse((target/'external').exists());self.assertFalse((target/'.atoms').exists())
    def test_docker_stdout_and_stderr_remain_separate(self):
        def frame(kind,data):return bytes([kind,0,0,0])+len(data).to_bytes(4,'big')+data
        self.assertEqual(unpack_docker_stream(frame(1,b'SQL')+frame(2,b'warning')),(b'SQL',b'warning'))
    def test_truncated_database_dump_is_rejected(self):
        with self.assertRaises(ValueError):unpack_docker_stream(bytes([1,0,0,0,0,0,0,20])+b'SQL')
