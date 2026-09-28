from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "process-bilibili-course" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from favorite_pipeline import (  # noqa: E402
    curate_source, import_text, likely_duplicate, make_basic_triage, review_source, transcribe_source,
)
from knowledge_db import (  # noqa: E402
    add_topic, add_triage, add_unit, connect, create_job, get_job, knowledge_tree,
    link_topic_unit, link_unit_source, list_jobs, requeue_interrupted_jobs, retry_job, review_queue, search,
    set_status, source_detail, update_job, update_triage_questions, upsert_source,
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

    def test_xiaohongshu_share_token_is_memory_only(self):
        captured = {}
        result = import_text(
            self.db,
            "https://www.xiaohongshu.com/discovery/item/6a897e21000000003300c059?xsec_token=secret-share-token&xsec_source=pc_share",
            transcribe=True,
            source_url_sink=lambda source_id, url: captured.update({source_id: url}),
        )
        source_id = result["source_ids"][0]
        stored = self.db.execute(
            "SELECT canonical_url FROM source_items WHERE id=?", (source_id,)
        ).fetchone()[0]
        payload = self.db.execute(
            "SELECT payload FROM processing_jobs WHERE source_item_id=?", (source_id,)
        ).fetchone()[0]
        self.assertEqual(
            stored,
            "https://www.xiaohongshu.com/discovery/item/6a897e21000000003300c059",
        )
        self.assertIn("secret-share-token", captured[source_id])
        self.assertNotIn("secret-share-token", stored + payload)

    def test_transcription_passes_share_url_through_stdin(self):
        source_id = upsert_source(self.db, {
            "platform": "xiaohongshu",
            "canonical_url": "https://www.xiaohongshu.com/discovery/item/abc",
        })
        share_url = "https://www.xiaohongshu.com/discovery/item/abc?xsec_token=secret"
        with patch("favorite_pipeline.subprocess.run", return_value=SimpleNamespace(returncode=1, stdout="failed")) as run:
            with self.assertRaises(RuntimeError):
                transcribe_source(self.db, self.root, source_id, source_url=share_url)
        self.assertEqual(run.call_args.kwargs["input"], share_url)
        self.assertNotIn("secret", " ".join(run.call_args.args[0]))
        self.assertEqual(run.call_args.kwargs["env"]["PYTHONIOENCODING"], "utf-8")

    def test_basic_triage_samples_full_transcript(self):
        text = "今天介绍流程。投递时记录岗位和进度。经历要用数据证明结果。面试提前准备三个故事。下一期再展开。"
        timed = ("[00:00:01.000] 今天介绍流程\n[00:00:12.300] 投递时记录岗位和进度\n"
                 "[00:01:20.500] 经历要用数据证明结果\n[00:02:10.000] 面试提前准备三个故事\n"
                 "[00:03:40.000] 下一期再展开\n")
        data = make_basic_triage(text, timed, "示例")
        self.assertLessEqual(len(data["summary_50"]), 50)
        self.assertEqual(
            [row["time"] for row in data["evidence"]],
            ["00:00:12", "00:01:20", "00:02:10"],
        )
        self.assertNotIn("下一期", "".join(data[f"point_{index}"] for index in range(1, 4)))
        self.assertEqual(data["key_questions"][0]["question"], "怎么寻找和管理投递岗位？")
        self.assertEqual(data["key_questions"][0]["evidence"][0]["time"], "00:00:12")
        self.assertEqual(data["ai_mode"], "basic")

    def test_questions_are_saved_edited_and_searchable(self):
        source_id = self.source()
        add_triage(self.db, source_id, {
            "summary_50": "①如何准备群面；②准备什么故事；③如何阶段总结",
            "key_questions": [{
                "question": "群面应该如何准备？",
                "answer": "不必抢主导，但要主动总结讨论进展并提炼框架。",
                "evidence": [{"time": "01:34", "excerpt": "主动做阶段总结，提炼框架思路。"}],
            }],
        })
        detail = source_detail(self.db, source_id)
        self.assertEqual(detail["key_questions"][0]["question"], "群面应该如何准备？")
        self.assertIn("t=94", detail["key_questions"][0]["evidence"][0]["source_url_at"])
        self.assertEqual(search(self.db, "群面怎么准备", 5)[0]["id"], source_id)
        updated = update_triage_questions(self.db, source_id, [{
            "question": "群面需要抢主导吗？",
            "answer": "不需要，重点是推进讨论并完成阶段总结。",
            "evidence": [{"time": "01:34", "excerpt": "主动做阶段总结。"}],
        }])
        self.assertEqual(updated["key_questions"][0]["question"], "群面需要抢主导吗？")

    def test_duplicate_candidates_require_strong_title_similarity(self):
        source = {
            "title": "手握12个offer的秘诀就是我太会秋招了",
            "canonical_url": "https://www.xiaohongshu.com/discovery/item/abc",
        }
        self.assertFalse(likely_duplicate(source, {
            "title": "R语言入门 多元线性回归与共线性处理",
            "canonical_url": "https://www.bilibili.com/video/BV123",
        }))
        self.assertTrue(likely_duplicate(source, {
            "title": "手握12个offer的秘诀就是我太会秋招了！完整版",
            "canonical_url": "https://www.bilibili.com/video/BV456",
        }))

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

    def test_knowledge_tree_supports_nested_topics_and_shared_units(self):
        first = add_unit(self.db, {"title": "残差诊断", "status": "approved"})
        second = add_unit(self.db, {"title": "VIF 检查", "status": "approved"})
        root = add_topic(self.db, "回归分析", description="从建模到诊断")
        diagnostics = add_topic(self.db, "模型诊断", parent_id=root)
        link_topic_unit(self.db, diagnostics, first, 1)
        link_topic_unit(self.db, diagnostics, second, 2)
        link_topic_unit(self.db, root, first, 1)

        tree = knowledge_tree(self.db)
        self.assertEqual(tree[0]["title"], "回归分析")
        self.assertEqual(tree[0]["units"][0]["title"], "残差诊断")
        self.assertEqual(
            [unit["title"] for unit in tree[0]["children"][0]["units"]],
            ["残差诊断", "VIF 检查"],
        )

    def test_openai_compatible_triage_parses_json(self):
        def responder(request: httpx.Request) -> httpx.Response:
            self.assertNotIn(b"secret-key", request.content)
            content = json.dumps({
                "questions": [
                    {"question": "第一步应该做什么？", "answer": "先定义问题。", "evidence": [{"time": "00:10", "excerpt": "定义问题"}]},
                    {"question": "执行时怎么拆分？", "answer": "把工作拆分为具体步骤。", "evidence": [{"time": "00:40", "excerpt": "拆分步骤"}]},
                    {"question": "最后如何确认结果？", "answer": "完成后复核最终结果。", "evidence": [{"time": "01:20", "excerpt": "复核结果"}]},
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
        self.assertEqual(data["key_questions"][0]["question"], "第一步应该做什么？")


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
            self.assertEqual(upgraded.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0], "4")
            upgraded.close()
            self.assertEqual(len(list((path.parent / "backups").glob("*.db"))), 1)


if __name__ == "__main__":
    unittest.main()
