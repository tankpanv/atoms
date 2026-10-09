import tempfile
import unittest
from pathlib import Path
from project_artifacts import artifacts, artifact_file


class ArtifactTests(unittest.TestCase):
    def test_generated_binary_is_downloadable_without_exposing_private_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'dist').mkdir()
            (root/'dist/report.pptx').write_bytes(b'PK document')
            (root/'dist/.env').write_text('secret')
            (root/'dist/index.js').write_text('source')
            (root/'dist/empty.pdf').touch()
            (root/'secret.txt').write_text('secret')
            (root/'dist/escape.txt').symlink_to(root/'secret.txt')
            self.assertEqual([e['path'] for e in artifacts(root)],['dist/report.pptx'])
            self.assertEqual(artifact_file(root,'dist/report.pptx').read_bytes(),b'PK document')
            for invalid in ('../secret.txt','dist/escape.txt','dist/.env','/etc/passwd'):
                with self.assertRaises(ValueError):artifact_file(root,invalid)

    def test_symlinked_output_directory_is_never_served(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'elsewhere').mkdir()
            (root/'elsewhere/secret.pdf').write_bytes(b'private')
            (root/'dist').symlink_to(root/'elsewhere')
            self.assertEqual(artifacts(root),[])
