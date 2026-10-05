from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "process-bilibili-course" / "scripts"))

from web_app import answer_from_cards, make_server  # noqa: E402
from knowledge_db import add_unit, connect, link_unit_source, upsert_source  # noqa: E402


class WebAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = make_server(Path(self.temp.name), 18920)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def test_health_import_and_dashboard(self):
        with httpx.Client(base_url=self.base, timeout=5) as client:
            self.assertTrue(client.get("/api/health").json()["ok"])
            response = client.post("/api/import", json={
                "text": "https://www.bilibili.com/video/BV1234567890", "transcribe": False,
            })
            self.assertEqual(response.status_code, 201)
            source_id = response.json()["source_ids"][0]
            duplicate = client.post("/api/import", json={
                "text": "https://www.bilibili.com/video/BV1234567890", "transcribe": False,
            }).json()["source_ids"][0]
            self.assertEqual(source_id, duplicate)
            queued = client.post(f"/api/items/{source_id}/transcribe", json={}).json()
            self.assertIsInstance(queued["job_id"], int)
            dashboard = client.get("/api/dashboard").json()
            self.assertEqual(dashboard["sources"], 1)
            self.assertEqual(client.get("/api/knowledge-tree").json(), [])
            no_answer = client.get("/api/search", params={"q": "完全不存在的答案"}).json()
            self.assertEqual(no_answer["answer"], "没有直接答案")
            client.put("/api/settings/llm", json={
                "base_url": "https://example.test/v1", "model": "demo", "api_key": "never-write-this-key",
            })
            self.assertNotIn(b"never-write-this-key", (Path(self.temp.name) / "知识库" / "knowledge.db").read_bytes())
            self.assertEqual(client.get("/").status_code, 200)

    def test_basic_search_answer_uses_curated_card_content(self):
        answer = answer_from_cards([{
            "kind": "knowledge_unit", "title": "检查回归模型",
            "when_to_use": "模型建立后", "steps": ["检查残差图", "检查 QQ 图"],
            "constraints": ["结合数据背景判断"],
        }])
        self.assertIn("1. 检查残差图", answer)
        self.assertIn("注意：", answer)

    def test_basic_search_answer_uses_matching_source_question(self):
        answer = answer_from_cards([{
            "kind": "source",
            "key_questions": [{
                "question": "群面应该如何准备？",
                "answer": "主动推进讨论，并完成阶段总结。",
                "evidence": [],
            }],
        }], "群面怎么准备")
        self.assertIn("主动推进讨论", answer)

    def test_basic_search_does_not_treat_question_only_as_an_answer(self):
        answer = answer_from_cards([{
            "kind": "source",
            "key_questions": [{
                "question": "群面应该如何准备？",
                "answer": "",
                "answer_status": "question_only",
                "evidence": [],
            }],
        }], "群面怎么准备")
        self.assertEqual(answer, "没有直接答案")

    def test_xiaohongshu_share_url_reaches_worker_memory(self):
        share_url = "https://www.xiaohongshu.com/discovery/item/abcdef1234567890?xsec_token=secret"
        with httpx.Client(base_url=self.base, timeout=5) as client:
            response = client.post("/api/import", json={"text": share_url, "transcribe": True})
        source_id = response.json()["source_ids"][0]
        self.assertEqual(self.server.state.worker.source_url(source_id), share_url)

    def test_topic_creation_and_primary_unit_assignment(self):
        db = connect(Path(self.temp.name) / "知识库" / "knowledge.db")
        try:
            unit_id = add_unit(db, {"title": "利润来源与成本控制", "status": "approved"})
        finally:
            db.close()
        with httpx.Client(base_url=self.base, timeout=5) as client:
            root = client.post("/api/topics", json={"title": "零售商业分析"}).json()["id"]
            child = client.post("/api/topics", json={
                "title": "成本结构", "parent_id": root,
            }).json()["id"]
            response = client.put(f"/api/units/{unit_id}/topic", json={"topic_id": child})
            self.assertEqual(response.status_code, 200)
            tree = client.get("/api/knowledge-tree").json()
        root_row = next(row for row in tree if row["id"] == root)
        self.assertEqual(root_row["children"][0]["id"], child)
        self.assertEqual(root_row["children"][0]["units"][0]["id"], unit_id)

    def test_organization_proposal_is_reviewed_before_writing_a_topic_tree(self):
        db = connect(Path(self.temp.name) / "知识库" / "knowledge.db")
        try:
            source_id = upsert_source(db, {
                "platform": "bilibili", "canonical_url": "https://www.bilibili.com/video/BVorganize001",
                "title": "新鲜零食的商业逻辑", "status": "curated",
            })
            first = add_unit(db, {"title": "利润来源与成本控制", "status": "approved"})
            second = add_unit(db, {"title": "赛道内卷与未来隐忧", "status": "approved"})
            link_unit_source(db, first, source_id)
            link_unit_source(db, second, source_id)
        finally:
            db.close()
        with httpx.Client(base_url=self.base, timeout=10) as client:
            proposal = client.post(f"/api/items/{source_id}/organization-proposal", json={})
            self.assertEqual(proposal.status_code, 200)
            plan = proposal.json()["plan"]
            self.assertEqual(client.get("/api/knowledge-tree").json()[0]["id"], 0)
            self.assertEqual(plan["architecture_type"], "topic_dossier")
            result = client.post(f"/api/items/{source_id}/organization", json={"plan": plan})
            self.assertEqual(result.status_code, 200)
            tree = client.get("/api/knowledge-tree").json()
        self.assertNotEqual(tree[0]["id"], 0)
        self.assertEqual(set(result.json()["unit_ids"]), {first, second})

    def test_second_server_cannot_reuse_the_same_port(self):
        second = make_server(Path(self.temp.name), self.server.server_address[1])
        try:
            self.assertNotEqual(second.server_address[1], self.server.server_address[1])
        finally:
            second.server_close()

    def test_local_ollama_profile_persists_without_a_key(self):
        with httpx.Client(base_url=self.base, timeout=5) as client:
            client.put("/api/settings/llm", json={
                "base_url": "http://127.0.0.1:11434/v1", "model": "qwen3:4b", "api_key": "ollama",
            })
        profile = Path(self.temp.name) / "知识库" / "local_model.json"
        self.assertEqual(json.loads(profile.read_text(encoding="utf-8")), {
            "base_url": "http://127.0.0.1:11434/v1", "model": "qwen3:4b",
        })
        restored = make_server(Path(self.temp.name), 18980)
        try:
            self.assertTrue(restored.state.settings.configured)
            self.assertFalse(restored.state.settings.public()["has_api_key"])
            self.assertEqual(restored.state.settings.model, "qwen3:4b")
        finally:
            restored.server_close()

    def test_cloud_key_uses_credential_manager_not_workspace_files(self):
        secret = "credential-manager-secret"
        with patch("web_app.store_api_key") as store:
            self.server.state.update_settings({
                "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                "model": "gemini-3.8-flash", "fallback_model": "gemini-3.1-flash-lite",
                "api_key": secret,
            })
            self.server.state.remember_cloud_settings()
        store.assert_called_once_with(secret)
        profile = Path(self.temp.name) / "知识库" / "cloud_model.json"
        self.assertEqual(json.loads(profile.read_text(encoding="utf-8")), {
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
            "model": "gemini-3.8-flash",
            "fallback_model": "gemini-3.1-flash-lite",
        })
        self.assertNotIn(secret.encode(), profile.read_bytes())
        self.assertNotIn(secret.encode(), (Path(self.temp.name) / "知识库" / "knowledge.db").read_bytes())

    def test_cloud_credential_profile_restores_on_startup(self):
        profile = Path(self.temp.name) / "知识库" / "cloud_model.json"
        profile.parent.mkdir(parents=True, exist_ok=True)
        profile.write_text(json.dumps({
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
            "model": "gemini-3.8-flash",
        }), encoding="utf-8")
        with patch("web_app.load_api_key", return_value="credential-manager-secret"):
            restored = make_server(Path(self.temp.name), 18980)
        try:
            self.assertTrue(restored.state.settings.configured)
            self.assertEqual(restored.state.settings.model, "gemini-3.8-flash")
            self.assertEqual(restored.state.settings.fallback_model, "")
            self.assertTrue(restored.state.public_settings()["key_saved_on_device"])
        finally:
            restored.server_close()


if __name__ == "__main__":
    unittest.main()
