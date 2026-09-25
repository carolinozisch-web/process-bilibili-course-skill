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


if __name__ == "__main__":
    unittest.main()
