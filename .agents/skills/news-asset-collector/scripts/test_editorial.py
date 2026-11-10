import copy
import json
from pathlib import Path
import tempfile
import unittest
import collector
import editorial


class EditorialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        media = self.root/'sample.webm'
        media.write_bytes(b'offline-reference-fixture')
        self.news = {'selection_status': 'confirmed', 'selected_story_ids': ['news1']}
        self.assets = {'assets': [{'id': 'a1', 'news_id': 'news1', 'status': 'acquired', 'media_type': 'video', 'file_path': 'sample.webm', 'sha256': collector.digest(media), 'technical': {'duration_seconds': 8}}]}
        collector.atomic_json(self.root/'news.json', self.news)
        collector.atomic_json(self.root/'assets.json', self.assets)
        self.plan = {'title': '离线测试报道', 'status': 'draft', 'paragraphs': [{'id': 'p001', 'news_ids': ['news1'], 'text': '厂商公布了新产品。', 'sources': [{'url': 'https://example.com/announcement', 'support': '官方公告；离线虚拟示例。'}], 'shots': [{'asset_id': 'a1', 'role': 'illustration', 'note': '只作背景，非性能证据。', 'in_seconds': 0, 'out_seconds': 4}], 'gaps': []}]}
        self.input = self.root/'input.json'

    def save(self):
        collector.atomic_json(self.input, self.plan)
        return editorial.run(self.root, self.input, apply=True)

    def test_export_and_preview(self):
        collector.atomic_json(self.input, self.plan)
        editorial.run(self.root, self.input)
        self.assertFalse((self.root/'report.md').exists())
        self.save()
        self.assertEqual(editorial.run(self.root, check=True)['status'], 'draft_valid')
        board = editorial.load(self.root/'storyboard.json')
        self.assertNotIn('text', board['paragraphs'][0])
        self.assertEqual(board['paragraphs'][0]['shots'][0]['file_path'], 'sample.webm')

    def test_missing_source_asset_and_unselected_story(self):
        for field, value in [('sources', []), ('news_ids', ['other']), ('shots', [{'asset_id': 'missing'}])]:
            plan = copy.deepcopy(self.plan)
            plan['paragraphs'][0][field] = value
            with self.assertRaises(ValueError):
                editorial.validate(self.root, plan)

    def test_video_timecodes(self):
        for end in (9, -1, float('nan'), True):
            plan = copy.deepcopy(self.plan)
            plan['paragraphs'][0]['shots'][0]['out_seconds'] = end
            with self.assertRaises(ValueError):
                editorial.validate(self.root, plan)

    def test_gap_can_be_explicit_and_approval_cannot_be_faked(self):
        self.plan['paragraphs'][0].update(shots=[], gaps=['缺少功能演示画面'])
        self.save()
        self.plan['status'] = 'approved'
        with self.assertRaises(ValueError):
            self.save()

    def test_asset_tamper_and_stale_inputs(self):
        self.save()
        (self.root/'sample.webm').write_bytes(b'tampered')
        with self.assertRaises(ValueError):
            editorial.run(self.root, check=True)
        (self.root/'sample.webm').write_bytes(b'offline-reference-fixture')
        self.news['extra'] = 'changed'
        collector.atomic_json(self.root/'news.json', self.news)
        with self.assertRaises(ValueError):
            editorial.run(self.root, check=True)

    def test_edits_backup_and_markdown_change_detected(self):
        self.save()
        (self.root/'report.md').write_text('user edits', encoding='utf-8')
        with self.assertRaises(ValueError):
            editorial.run(self.root, check=True)
        self.plan['paragraphs'][0]['text'] = '修订正文。'
        self.save()
        backups = list((self.root/'artifacts').glob('editorial-*/report.md'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding='utf-8'), 'user edits')

    def test_recorded_text_approval_survives_visual_updates_only(self):
        self.plan['text_approval'] = {'status': 'approved', 'user_response': '无需修改，按此稿继续', 'recorded_at': '2026-11-10T10:00:00+08:00', 'approved_text_sha256': editorial.narration_digest(self.plan)}
        self.plan['paragraphs'][0]['shots'][0]['note'] = '更新画面说明，正文不变。'
        self.save()
        self.assertIn('正文已确认', (self.root/'report.md').read_text(encoding='utf-8'))
        editorial.run(self.root, check=True)
        self.plan['paragraphs'][0]['text'] = '未经确认的新正文。'
        with self.assertRaises(ValueError):
            self.save()


if __name__ == '__main__':
    unittest.main(verbosity=2)
