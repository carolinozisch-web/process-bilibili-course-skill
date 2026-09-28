from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "process-bilibili-course" / "scripts"))

from web_app import answer_from_cards, make_server  # noqa: E402


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

    def test_xiaohongshu_share_url_reaches_worker_memory(self):
        share_url = "https://www.xiaohongshu.com/discovery/item/abcdef1234567890?xsec_token=secret"
        with httpx.Client(base_url=self.base, timeout=5) as client:
            response = client.post("/api/import", json={"text": share_url, "transcribe": True})
        source_id = response.json()["source_ids"][0]
        self.assertEqual(self.server.state.worker.source_url(source_id), share_url)

    def test_second_server_cannot_reuse_the_same_port(self):
        second = make_server(Path(self.temp.name), self.server.server_address[1])
        try:
            self.assertNotEqual(second.server_address[1], self.server.server_address[1])
        finally:
            second.server_close()


if __name__ == "__main__":
    unittest.main()
