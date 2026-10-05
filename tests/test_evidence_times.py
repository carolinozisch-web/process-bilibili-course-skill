from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'process-bilibili-course' / 'scripts'))

from favorite_pipeline import curate_source
from knowledge_db import add_triage, add_unit, connect, link_unit_source, source_detail, update_triage_questions, upsert_source
from llm_client import LLMError, triage_with_ai
from repair_evidence_times import apply_repairs, plan_repairs
from transcript_evidence import EvidenceError, bind_outline, resolve_evidence, timestamp_seconds, transcript_segments


class StubClient:
    def __init__(self, evidence):
        self.evidence = evidence
        self.prompts = []

    def complete_json(self, system, prompt):
        self.prompts.append(prompt)
        return {'overview': '说明零食保质期与成本的关系。', 'sections': [
            {'title': '保质期', 'summary': '短保产品需要控制损耗。', 'points': [], 'evidence': [self.evidence]}]}


class EvidenceTimeTests(unittest.TestCase):
    transcript = '[00:02:00.600] 产品保质期只有五天\n[00:03:04.300] 需要控制损耗'

    def test_strict_parser_preserves_real_hours_and_rejects_bad_fields(self):
        self.assertEqual(timestamp_seconds('01:02:03.400'), 3723.4)
        self.assertEqual(timestamp_seconds('02:00'), 120)
        self.assertEqual(timestamp_seconds(96.75999999999999), 96.75999999999999)
        for value in ('00:99:00', '-12', 'nan', 'inf', True, '1:2:3', '00:12abc'):
            self.assertIsNone(timestamp_seconds(value), value)

    def test_model_id_ignores_model_written_hours(self):
        client = StubClient({'segment_id': 'S000001', 'time': '02:00:00', 'excerpt': '伪造文字'})
        data = triage_with_ai(client, self.transcript, duration=617)
        self.assertEqual(data['evidence'][0]['time'], '00:02:00.600')
        self.assertEqual(data['evidence'][0]['excerpt'], '产品保质期只有五天')
        self.assertIn('[S000001]', client.prompts[0])
        self.assertNotIn('00:02:00.600', client.prompts[0])

    def test_unknown_id_and_ungrounded_legacy_quote_are_rejected(self):
        for evidence in ({'segment_id': 'S999999'}, {'time': '02:00:00', 'excerpt': '原文不存在的内容'}):
            with self.assertRaises(LLMError):
                triage_with_ai(StubClient(evidence), self.transcript, duration=617)

    def test_legacy_quote_rebinds_to_real_time_not_division(self):
        data = triage_with_ai(StubClient({'time': '02:00:00', 'excerpt': '产品保质期只有五天'}),
                              self.transcript, duration=617)
        self.assertEqual(data['evidence'][0]['time'], '00:02:00.600')

    def test_duplicate_quote_is_not_guessed(self):
        segments = transcript_segments('[00:01] 先确定岗位方向\n[00:02] 先确定岗位方向')
        self.assertIsNone(resolve_evidence({'time': '01:00:00', 'excerpt': '先确定岗位方向'}, segments))

    def test_multiline_quote_uses_first_supporting_row(self):
        segments = transcript_segments('[00:02:00.600] 产品保质期\n[00:02:03.400] 只有五天')
        bound = resolve_evidence({'excerpt': '产品保质期 只有五天'}, segments)
        self.assertEqual(bound['start_seconds'], 120.6)

    def test_true_long_video_time_is_not_shifted(self):
        data = triage_with_ai(StubClient({'segment_id': 'S000001'}),
                              '[02:00:00.000] 产品保质期只有五天', duration=8000)
        self.assertEqual(data['evidence'][0]['time'], '02:00:00.000')

    def test_bad_transcript_time_is_rejected_before_model_call(self):
        client = StubClient({'segment_id': 'S000001'})
        with self.assertRaises(LLMError):
            triage_with_ai(client, '[02:00:00] 产品保质期只有五天', duration=617)
        self.assertEqual(client.prompts, [])

    def test_reduction_keeps_global_ids_and_rebinds_times(self):
        client = StubClient({'segment_id': 'S000001'})
        replies = [
            client.complete_json('', ''),
            {'overview': '成本控制', 'sections': [{'title': '损耗', 'summary': '控制损耗。', 'points': [],
                'evidence': [{'segment_id': 'S000002'}]}]},
            {'overview': '保质期和损耗共同影响成本。', 'sections': [{'title': '成本', 'summary': '需控制成本。', 'points': [],
                'evidence': [{'segment_id': 'S000002', 'time': '03:04:00'}]}]},
        ]
        with patch.object(client, 'complete_json', side_effect=replies), patch('llm_client.chunk_text',
                return_value=['[S000001] 产品保质期只有五天', '[S000002] 需要控制损耗']):
            data = triage_with_ai(client, self.transcript, duration=617)
        self.assertEqual(data['evidence'][0]['time'], '00:03:04.300')

    def test_model_cannot_cite_another_chunk(self):
        client = StubClient({'segment_id': 'S000002'})
        with patch('llm_client.chunk_text', return_value=['[S000001] 产品保质期只有五天', '[S000002] 需要控制损耗']):
            with self.assertRaises(LLMError):
                triage_with_ai(client, self.transcript, duration=617)

    def test_database_guards_and_repair_preserve_review_and_node_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transcript = root / 'transcript.md'
            transcript.write_text(self.transcript, encoding='utf-8')
            db = connect(root / 'knowledge.db')
            try:
                source = upsert_source(db, {'platform': 'bilibili', 'canonical_url': 'https://example.test/video',
                    'status': 'curated', 'duration_seconds': 617, 'transcript_path': str(transcript),
                    'timestamped_transcript_path': str(transcript)})
                db.execute("UPDATE source_items SET review_decision='approve' WHERE id=?", (source,))
                db.commit()
                client = StubClient({'segment_id': 'S000001'})
                data = triage_with_ai(client, self.transcript, duration=617)
                add_triage(db, source, data)
                self.assertEqual(source_detail(db, source)['video_outline']['sections'][0]['evidence'][0]['segment_id'], 'S000001')
                unit = add_unit(db, {'title': '保质期', 'status': 'approved'})
                with self.assertRaises(EvidenceError):
                    link_unit_source(db, unit, source, segment_start=7200)
                with self.assertRaises(ValueError):
                    link_unit_source(db, unit, source, segment_start=200, segment_end=100)
                link_unit_source(db, unit, source, segment_start=120.6)
                with self.assertRaises(EvidenceError):
                    update_triage_questions(db, source, [{'question': '如何控制损耗？', 'answer': '控制保质期。',
                        'evidence': [{'time': '02:00:00', 'excerpt': '产品保质期只有五天'}]}])
                data['video_outline']['sections'][0]['evidence'][0]['time'] = '02:00:00'
                with self.assertRaises(EvidenceError):
                    add_triage(db, source, data)
                db.execute('UPDATE triage SET video_outline_json=?, usable_content_start=? WHERE source_item_id=?',
                    (json.dumps(data['video_outline']), '02:00:00', source))
                db.execute('UPDATE unit_sources SET segment_start=7200 WHERE knowledge_unit_id=?', (unit,))
                db.commit()
                with self.assertRaises(EvidenceError):
                    curate_source(db, root, source)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM knowledge_units').fetchone()[0], 1)
                plans, unresolved = plan_repairs(db, root)
                self.assertEqual(unresolved, [])
                backup = apply_repairs(db, plans, root / 'backups')
                self.assertTrue(backup.is_file())
                detail = source_detail(db, source)
                self.assertEqual(detail['units'][0]['id'], unit)
                self.assertEqual(detail['units'][0]['segment_start'], 120.6)
                self.assertEqual(detail['status'], 'curated')
                self.assertEqual(detail['review_decision'], 'approve')
                self.assertEqual(plan_repairs(db, root), ([], []))
                with closing(sqlite3.connect(backup)) as old:
                    self.assertEqual(old.execute('SELECT segment_start FROM unit_sources').fetchone()[0], 7200)
            finally:
                db.close()


if __name__ == '__main__':
    unittest.main()
