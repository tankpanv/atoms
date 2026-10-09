"""Validate trusted selection and immutable task skill snapshots."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
import experts


class ExpertSkillTests(unittest.TestCase):
    def test_task_keeps_selected_skill_version_after_catalogue_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'website').mkdir()
            skill = root / 'website' / 'SKILL.md'
            skill.write_text('first approved design instructions')
            (root / 'catalog.json').write_text(json.dumps([{'id': 'website', 'skill': 'website', 'name': 'Website', 'version': '1.0'}]))
            with patch.object(experts, 'ROOT', root):
                snapshot = experts.snapshot_experts(['website'])
                skill.write_text('replacement instructions for future jobs')
                context = experts.skill_context(snapshot)
                self.assertIn('first approved design instructions', context)
                self.assertNotIn('replacement instructions', context)
                self.assertEqual(snapshot[0]['sha256'], hashlib.sha256(b'first approved design instructions').hexdigest())
                self.assertEqual(snapshot[0]['version'], '1.0')
                self.assertIn('replacement instructions', experts.skill_context(experts.snapshot_experts(['website'])))

    def test_untrusted_id_cannot_load_arbitrary_file(self):
        for name in ['../../secret', '/etc/passwd', 'missing-expert']:
            with self.subTest(name=name), self.assertRaises(HTTPException) as raised:
                experts.snapshot_experts([name])
            self.assertEqual(raised.exception.status_code, 422)

    def test_selection_limit_and_duplicate_handling(self):
        ids = [item['id'] for item in experts.catalogue()]
        self.assertEqual(experts.validate_experts([ids[0], ids[0]]), [ids[0]])
        with self.assertRaises(HTTPException):
            experts.validate_experts(ids + [ids[0]])
        self.assertEqual(experts.skill_context([]), '')

    def test_every_expert_has_loadable_skill_and_real_examples(self):
        catalogue = experts.catalogue()
        self.assertEqual(len({item['id'] for item in catalogue}), len(catalogue))
        for item in catalogue:
            with self.subTest(expert=item['id']):
                snapshot = experts.snapshot_experts([item['id']])[0]
                self.assertGreater(len(snapshot['instructions']), 100)
                for example in item['examples']:
                    document = (experts.ROOT / 'examples' / example['file']).read_text()
                    self.assertIn('<!doctype html>', document.lower())
