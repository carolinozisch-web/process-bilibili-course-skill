from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "process-bilibili-course" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from favorite_pipeline import curate_source, make_basic_triage, review_source  # noqa: E402
from knowledge_db import (  # noqa: E402
    add_triage, add_unit, connect, create_job, get_job, link_unit_source,
    list_jobs, requeue_interrupted_jobs, retry_job, review_queue, search,
    set_status, source_detail, update_job, upsert_source,
)
from llm_client import LLMSettings, OpenAICompatibleClient, triage_with_ai  # noqa: E402


class KnowledgeLayerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "知识库" / "knowledge.db"
        self.db = connect(self.db_path)
        self.now = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def source(self, url="https://www.bilibili.com/video/BV1234567890", hours=0):
        return upsert_source(self.db, {
            "platform": "bilibili", "canonical_url": url,
            "saved_at": (self.now - timedelta(hours=hours)).isoformat(),
        })

    def triage(self, source_id):
        add_triage(self.db, source_id, {
            "summary_50": "①先定义问题；②分步执行；③检查结果",
            "point_1": "先定义问题", "point_2": "分步执行", "point_3": "检查结果",
            "evidence": [{"time": "00:12", "point": "先定义问题"}],
        })

    def test_recent_history_and_explicit_approval_gate(self):
        recent = self.source(hours=2)
        old = self.source("https://www.bilibili.com/video/BV0987654321", hours=72)
        self.triage(recent)
        self.triage(old)
        queues = review_queue(self.db, self.now.isoformat())
        self.assertEqual([row["id"] for row in queues["recent"]], [recent])
        self.assertEqual([row["id"] for row in queues["historical"]], [old])
        unit_id = add_unit(self.db, {"title": "问题拆解法", "status": "approved"})
        with self.assertRaises(ValueError):
            link_unit_source(self.db, unit_id, recent)
        review_source(self.db, recent, "approve")
        link_unit_source(self.db, unit_id, recent, segment_start=12)
        self.assertEqual(len(source_detail(self.db, recent)["units"]), 1)

    def test_duplicate_job_and_restart_recovery(self):
        source_id = self.source()
        first = create_job(self.db, source_id, "transcribe")
        second = create_job(self.db, source_id, "transcribe")
        self.assertEqual(first, second)
        update_job(self.db, first, status="running", phase="transcribe", progress=35)
        self.assertEqual(requeue_interrupted_jobs(self.db), 1)
        self.assertEqual(get_job(self.db, first)["status"], "queued")
        update_job(self.db, first, status="failed", error="network")
        retry_job(self.db, first)
        self.assertEqual(list_jobs(self.db)[0]["status"], "queued")

    def test_basic_triage_samples_full_transcript(self):
        text = "开头先定义问题。" + "中间步骤需要拆分。" * 20 + "最后必须检查结果。"
        timed = "[00:01] 开头证据\n[01:20] 中间证据\n[03:40] 结尾证据\n"
        data = make_basic_triage(text, timed, "示例")
        self.assertLessEqual(len(data["summary_50"]), 50)
        self.assertEqual([row["time"] for row in data["evidence"]], ["00:01", "01:20", "03:40"])
        self.assertEqual(data["ai_mode"], "basic")

    def test_search_returns_unit_with_timestamped_source(self):
        source_id = self.source()
        self.triage(source_id)
        set_status(self.db, source_id, "approved", "approve")
        unit_id = add_unit(self.db, {
            "title": "交叉表匹配", "when_to_use": "按共同字段匹配两张表",
            "steps": ["选择查找键", "返回目标列"], "keywords": ["XLOOKUP"], "status": "approved",
        })
        link_unit_source(self.db, unit_id, source_id, segment_start=65)
        results = search(self.db, "怎样使用XLOOKUP方法", 5)
        unit = next(row for row in results if row["kind"] == "knowledge_unit")
        self.assertIn("t=65", unit["sources"][0]["source_url_at"])

    def test_bilibili_part_and_timestamp_are_query_parameters(self):
        source_id = upsert_source(self.db, {
            "platform": "bilibili",
            "canonical_url": "https://www.bilibili.com/video/BV1234567890/#p=7",
            "status": "approved",
        })
        unit_id = add_unit(self.db, {"title": "VIF 检查", "status": "approved"})
        link_unit_source(self.db, unit_id, source_id, segment_start=305)
        linked = search(self.db, "VIF", 5)[0]["sources"][0]["source_url_at"]
        self.assertEqual(linked, "https://www.bilibili.com/video/BV1234567890/?p=7&t=305")

    def test_search_splits_long_chinese_question_terms(self):
        source_id = upsert_source(self.db, {
            "platform": "bilibili", "canonical_url": "https://www.bilibili.com/video/BVdiagnostic1",
            "title": "回归诊断", "status": "approved",
        })
        unit_id = add_unit(self.db, {
            "title": "用残差图和 QQ 图诊断线性回归",
            "when_to_use": "检查线性、等方差和正态性假设",
            "steps": ["检查残差图", "检查 QQ 图"],
            "keywords": ["模型诊断", "线性回归"], "status": "approved",
        })
        link_unit_source(self.db, unit_id, source_id, segment_start=30)
        results = search(self.db, "怎样判断线性回归模型是否可靠", 5)
        self.assertEqual(results[0]["title"], "用残差图和 QQ 图诊断线性回归")
        keys = [(row["kind"], row["id"]) for row in results]
        self.assertEqual(len(keys), len(set(keys)))

    def test_manual_curation_can_create_zero_or_more_units(self):
        transcript = self.root / "transcript.txt"
        transcript.write_text("先定义问题，再拆分步骤，最后检查结果。", encoding="utf-8")
        source_id = upsert_source(self.db, {
            "platform": "bilibili", "canonical_url": "https://www.bilibili.com/video/BVmanual0001",
            "transcript_path": str(transcript), "status": "approved",
        })
        result = curate_source(self.db, self.root, source_id, manual_units=[{
            "title": "三步检查法", "when_to_use": "执行复杂任务时",
            "steps": ["定义问题", "拆分步骤", "检查结果"], "status": "approved",
        }])
        self.assertEqual(len(result["unit_ids"]), 1)
        self.assertEqual(source_detail(self.db, source_id)["status"], "curated")

        empty_transcript = self.root / "empty-method.txt"
        empty_transcript.write_text("这是一段没有具体方法的介绍。", encoding="utf-8")
        empty_source = upsert_source(self.db, {
            "platform": "bilibili", "canonical_url": "https://www.bilibili.com/video/BVmanual0002",
            "transcript_path": str(empty_transcript), "status": "approved",
        })
        empty_result = curate_source(self.db, self.root, empty_source, manual_units=[])
        self.assertEqual(empty_result["unit_ids"], [])
        self.assertEqual(source_detail(self.db, empty_source)["status"], "curated")

    def test_openai_compatible_triage_parses_json(self):
        def responder(request: httpx.Request) -> httpx.Response:
            self.assertNotIn(b"secret-key", request.content)
            content = json.dumps({
                "points": [
                    {"text": "先定义问题", "time": "00:10"},
                    {"text": "拆分执行步骤", "time": "00:40"},
                    {"text": "复核最终结果", "time": "01:20"},
                ],
                "keywords": ["执行", "复核"], "topics": ["方法"],
            }, ensure_ascii=False)
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

        settings = LLMSettings("https://example.test/v1", "example-model", "secret-key")
        client = OpenAICompatibleClient(settings, httpx.MockTransport(responder))
        data = triage_with_ai(client, "[00:10] 定义问题\n[00:40] 拆分步骤\n[01:20] 复核结果", "示例")
        self.assertLessEqual(len(data["summary_50"]), 50)
        self.assertEqual(data["ai_mode"], "ai")
        self.assertEqual(data["evidence"][0]["time"], "00:10")


class MigrationTests(unittest.TestCase):
    def test_v1_database_is_backed_up_and_legacy_rows_are_not_approved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "knowledge.db"
            db = sqlite3.connect(path)
            db.executescript("""
                CREATE TABLE source_items (
                    id INTEGER PRIMARY KEY, platform TEXT NOT NULL, canonical_url TEXT NOT NULL UNIQUE,
                    platform_item_id TEXT, title TEXT NOT NULL DEFAULT '', author_name TEXT DEFAULT '',
                    author_type TEXT DEFAULT '', saved_at TEXT, imported_at TEXT NOT NULL,
                    duration_seconds INTEGER, transcript_path TEXT, timestamped_transcript_path TEXT,
                    metadata_path TEXT, status TEXT NOT NULL DEFAULT 'discovered', reviewed_at TEXT,
                    review_decision TEXT, error_message TEXT
                );
                CREATE TABLE triage (
                    source_item_id INTEGER PRIMARY KEY, summary_50 TEXT NOT NULL,
                    point_1 TEXT NOT NULL DEFAULT '', point_2 TEXT NOT NULL DEFAULT '', point_3 TEXT NOT NULL DEFAULT '',
                    keywords TEXT NOT NULL DEFAULT '[]', topic_candidates TEXT NOT NULL DEFAULT '[]',
                    source_signals TEXT NOT NULL DEFAULT '[]', possible_duplicates TEXT NOT NULL DEFAULT '[]',
                    new_points TEXT NOT NULL DEFAULT '[]', usable_content_start TEXT, created_at TEXT NOT NULL
                );
                INSERT INTO source_items(platform, canonical_url, imported_at, status, reviewed_at, review_decision)
                VALUES('bilibili','https://example.test/video','2026-01-01T00:00:00+00:00','curated',
                       '2026-01-01T00:00:00+00:00','legacy_curated');
            """)
            db.commit()
            db.close()
            upgraded = connect(path)
            row = upgraded.execute("SELECT status, reviewed_at, review_decision FROM source_items").fetchone()
            self.assertEqual(tuple(row), ("legacy_imported", None, "legacy_migration"))
            self.assertEqual(upgraded.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0], "2")
            upgraded.close()
            self.assertEqual(len(list((path.parent / "backups").glob("*.db"))), 1)


if __name__ == "__main__":
    unittest.main()
