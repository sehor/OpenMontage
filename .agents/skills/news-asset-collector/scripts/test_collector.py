import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import collector


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / 'project'
        self.input = self.base / 'input.json'
        self.record = json.loads((Path(__file__).parents[1] / 'examples/candidates.json').read_text())[0]
        self.source = self.base / 'original.PNG'
        # Minimal real 1x1 PNG fixture, no network or model required.
        import base64
        self.source.write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aV1sAAAAASUVORK5CYII='))
        self.record.update(local_path=str(self.source), brand='Example', event='Demo', scene='pixel')
        self.record.pop('link_only_reason')

    def run_items(self, records, apply=True):
        self.input.write_text(json.dumps(records), encoding='utf-8')
        with patch('socket.socket', side_effect=AssertionError('Network forbidden')):
            return collector.run(self.project, self.input, apply)

    def test_preview_has_no_side_effect(self):
        planned = self.run_items([self.record], False)
        self.assertTrue(planned[0]['file_path'].endswith('_001.PNG'))
        self.assertFalse(self.project.exists())

    def test_copy_backup_idempotency_and_provenance(self):
        items = self.run_items([self.record])
        item = items[0]
        self.assertEqual((self.project / item['file_path']).read_bytes(), self.source.read_bytes())
        self.assertEqual((self.project / item['backup_path']).read_bytes(), self.source.read_bytes())
        second = copy.deepcopy(self.record)
        second['id'] = 'second-source'
        second['source']['page_url'] = 'https://example.org/'
        self.run_items([self.record, second])
        manifest = json.loads((self.project / 'assets.json').read_text())
        self.assertEqual(len(manifest['assets']), 2)
        self.assertEqual(manifest['assets'][0]['file_path'], manifest['assets'][1]['file_path'])
        self.assertTrue(self.source.exists())

    def test_same_batch_duplicate(self):
        other = dict(self.record, id='other')
        items = self.run_items([self.record, other])
        self.assertEqual(items[0]['file_path'], items[1]['file_path'])

    def test_collision_preserves_existing(self):
        planned = self.run_items([self.record], False)[0]
        target = self.project / planned['file_path']
        target.parent.mkdir(parents=True)
        target.write_bytes(b'existing')
        item = self.run_items([self.record])[0]
        self.assertNotEqual(item['file_path'], planned['file_path'])
        self.assertEqual(target.read_bytes(), b'existing')

    def test_link_only(self):
        record = copy.deepcopy(self.record)
        record.pop('local_path')
        record['link_only_reason'] = 'Media endpoint returned 404'
        item = self.run_items([record])[0]
        self.assertIsNone(item['file_path'])
        self.assertEqual(item['status'], 'link_only')

    def test_distinct_bytes_same_name(self):
        other_file = self.base / 'other.PNG'
        other_file.write_bytes(b'different-fixture')
        other = dict(self.record, id='other', local_path=str(other_file))
        items = self.run_items([self.record, other])
        self.assertNotEqual(items[0]['file_path'], items[1]['file_path'])
        self.assertTrue(items[1]['file_path'].endswith('_002.PNG'))

    def test_manifest_and_tamper_protection(self):
        item = self.run_items([self.record])[0]
        (self.project / item['file_path']).write_bytes(b'tampered')
        with self.assertRaises(ValueError):
            self.run_items([self.record])
        manifest = self.project / 'assets.json'
        manifest.write_text('{"assets": []}')
        with self.assertRaises(ValueError):
            self.run_items([self.record])
        self.assertEqual(manifest.read_text(), '{"assets": []}')

    def test_no_licensing_required_or_invented(self):
        item = self.run_items([self.record])[0]
        self.assertEqual(item['status'], 'acquired')
        self.assertNotIn('licensing', item)
        manifest = json.loads((self.project / 'assets.json').read_text())
        self.assertNotIn('licensing', manifest['assets'][0])

    def test_supplied_legacy_licensing_preserved(self):
        record = copy.deepcopy(self.record)
        record['licensing'] = {'download_permission': 'unknown'}
        item = self.run_items([record])[0]
        self.assertEqual(item['status'], 'acquired')
        self.assertEqual(item['licensing'], record['licensing'])

    def test_changed_id_bytes_rejected(self):
        self.run_items([self.record])
        self.source.write_bytes(b'changed')
        with self.assertRaises(ValueError):
            self.run_items([self.record])

    def test_lock_and_escape(self):
        self.project.mkdir()
        lock = self.project / '.news-asset-collector.lock'
        lock.write_text('active')
        with self.assertRaises(FileExistsError):
            self.run_items([self.record])
        self.assertTrue(lock.exists())
        with self.assertRaises(ValueError):
            collector.safe(self.project.resolve(), '../outside')


if __name__ == '__main__':
    unittest.main(verbosity=2)
