#!/usr/bin/env python3
"""Local web interface for the saved-video knowledge inbox."""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from favorite_pipeline import JobWorker, curate_source, import_text, review_source, transcript_text
from knowledge_db import (
    connect, create_job, dashboard_counts, get_job, get_source, list_jobs, list_sources,
    list_units, log_search, record_search_feedback, retry_job, review_queue, search,
    source_detail, update_unit,
)
from llm_client import (
    LLMError, LLMSettings, OpenAICompatibleClient, answer_with_ai, expand_query,
)


MAX_BODY_BYTES = 1024 * 1024


class AppState:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self.db_path = self.workspace / "知识库" / "knowledge.db"
        self.assets = Path(__file__).resolve().parents[1] / "assets" / "web"
        self.settings = LLMSettings.from_environment()
        self.settings_lock = threading.Lock()
        db = connect(self.db_path)
        db.close()
        self.worker = JobWorker(self.db_path, self.workspace, self.client)

    def client(self) -> OpenAICompatibleClient | None:
        with self.settings_lock:
            settings = LLMSettings(self.settings.base_url, self.settings.model, self.settings.api_key)
        return OpenAICompatibleClient(settings) if settings.configured else None

    def update_settings(self, values: dict) -> dict:
        with self.settings_lock:
            self.settings.base_url = str(values.get("base_url", self.settings.base_url)).strip()
            self.settings.model = str(values.get("model", self.settings.model)).strip()
            if values.get("api_key"):
                self.settings.api_key = str(values["api_key"]).strip()
            if values.get("clear_api_key"):
                self.settings.api_key = ""
            return self.settings.public()


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, state: AppState):
        self.state = state
        super().__init__(address, handler)


class Handler(BaseHTTPRequestHandler):
    server: AppServer

    def log_message(self, format: str, *args) -> None:
        print(f"[web] {self.address_string()} {format % args}")

    def _json(self, value: object, status: int = 200) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_BODY_BYTES:
            raise ValueError("请求内容过大")
        if not length:
            return {}
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("请求必须是 JSON 对象")
        return value

    def _safe_host(self) -> bool:
        host = self.headers.get("Host", "").split(":", 1)[0].strip("[]").lower()
        return host in {"127.0.0.1", "localhost"}

    def _serve_asset(self, relative: str) -> None:
        name = relative or "index.html"
        path = (self.server.state.assets / name).resolve()
        if self.server.state.assets.resolve() not in path.parents and path != self.server.state.assets.resolve():
            self._error(404, "not found")
            return
        if not path.is_file():
            path = self.server.state.assets / "index.html"
        data = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", f"{mime}; charset=utf-8" if mime.startswith("text/") else mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if not self._safe_host():
            self._error(403, "仅允许本机访问")
            return
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self._serve_asset(parsed.path.lstrip("/"))
            return
        try:
            self._get_api(parsed.path, parse_qs(parsed.query))
        except (ValueError, LLMError) as exc:
            self._error(400, str(exc))
        except Exception as exc:
            self._error(500, f"服务器处理失败：{exc}")

    def do_POST(self) -> None:
        self._write_api("POST")

    def do_PUT(self) -> None:
        self._write_api("PUT")

    def _write_api(self, method: str) -> None:
        if not self._safe_host():
            self._error(403, "仅允许本机访问")
            return
        try:
            self._mutate_api(method, urlparse(self.path).path, self._body())
        except json.JSONDecodeError:
            self._error(400, "JSON 格式错误")
        except (ValueError, LLMError) as exc:
            self._error(400, str(exc))
        except Exception as exc:
            self._error(500, f"服务器处理失败：{exc}")

    def _get_api(self, path: str, query: dict[str, list[str]]) -> None:
        state = self.server.state
        db = connect(state.db_path)
        try:
            if path == "/api/health":
                self._json({"ok": True, "workspace": str(state.workspace), "database": str(state.db_path)})
            elif path == "/api/dashboard":
                self._json(dashboard_counts(db))
            elif path == "/api/items":
                queue_name = query.get("queue", [""])[0]
                if queue_name in {"recent", "historical"}:
                    self._json(review_queue(db).get(queue_name, []))
                elif queue_name == "review":
                    self._json(review_queue(db))
                else:
                    status = query.get("status", [None])[0]
                    self._json(list_sources(db, status=status, limit=200))
            elif match := re.fullmatch(r"/api/items/(\d+)", path):
                source_id = int(match.group(1))
                detail = source_detail(db, source_id)
                if not detail:
                    self._error(404, "没有找到这条收藏")
                    return
                text = transcript_text(detail, state.workspace)
                detail["transcript_excerpt"] = text[:8000]
                detail["transcript_truncated"] = len(text) > 8000
                self._json(detail)
            elif path == "/api/jobs":
                self._json(list_jobs(db))
            elif path == "/api/units":
                self._json(list_units(db))
            elif path == "/api/search":
                query_text = query.get("q", [""])[0].strip()
                if not query_text:
                    raise ValueError("请输入搜索问题")
                client = state.client()
                terms = [query_text]
                ai_error = None
                if client:
                    try:
                        terms.extend(expand_query(client, query_text))
                    except LLMError as exc:
                        ai_error = str(exc)
                merged, seen = [], set()
                for term in terms:
                    for result in search(db, term, 12):
                        key = (result["kind"], result["id"])
                        if key not in seen:
                            seen.add(key)
                            merged.append(result)
                merged = merged[:20]
                answer = "没有直接答案" if not merged else "已找到相关知识卡和原始来源。"
                if client and merged:
                    try:
                        answer = answer_with_ai(client, query_text, merged)
                    except LLMError as exc:
                        ai_error = str(exc)
                event_id = log_search(db, query_text, [row["id"] for row in merged if row["kind"] == "knowledge_unit"])
                self._json({"event_id": event_id, "answer": answer, "results": merged,
                            "mode": "ai" if client and not ai_error else "literal", "ai_error": ai_error})
            elif path == "/api/settings/llm":
                self._json(state.settings.public())
            else:
                self._error(404, "not found")
        finally:
            db.close()

    def _mutate_api(self, method: str, path: str, body: dict) -> None:
        state = self.server.state
        db = connect(state.db_path)
        try:
            if method == "POST" and path == "/api/import":
                result = import_text(db, str(body.get("text", "")), transcribe=bool(body.get("transcribe", True)))
                self._json(result, HTTPStatus.CREATED)
            elif method == "POST" and (match := re.fullmatch(r"/api/items/(\d+)/transcribe", path)):
                source_id = int(match.group(1))
                source = get_source(db, source_id)
                if not source:
                    raise ValueError("没有找到这条收藏")
                self._json({"job_id": create_job(db, source_id, "transcribe")}, HTTPStatus.ACCEPTED)
            elif method == "POST" and (match := re.fullmatch(r"/api/items/(\d+)/review", path)):
                self._json(review_source(db, int(match.group(1)), str(body.get("decision", ""))))
            elif method == "POST" and (match := re.fullmatch(r"/api/items/(\d+)/triage", path)):
                source_id = int(match.group(1))
                self._json({"job_id": create_job(db, source_id, "triage")}, HTTPStatus.ACCEPTED)
            elif method == "POST" and (match := re.fullmatch(r"/api/items/(\d+)/curate", path)):
                source_id = int(match.group(1))
                units = body.get("units")
                if units is not None:
                    if not isinstance(units, list):
                        raise ValueError("units 必须是数组")
                    self._json(curate_source(db, state.workspace, source_id, manual_units=units))
                elif state.client():
                    self._json({"job_id": create_job(db, source_id, "curate")}, HTTPStatus.ACCEPTED)
                else:
                    result = curate_source(db, state.workspace, source_id)
                    self._json(result, HTTPStatus.CONFLICT)
            elif method == "POST" and (match := re.fullmatch(r"/api/jobs/(\d+)/retry", path)):
                retry_job(db, int(match.group(1)))
                self._json({"ok": True})
            elif method == "PUT" and (match := re.fullmatch(r"/api/units/(\d+)", path)):
                self._json(update_unit(db, int(match.group(1)), body))
            elif method == "POST" and path == "/api/search-feedback":
                record_search_feedback(db, int(body.get("event_id")), str(body.get("feedback", "")),
                                       body.get("clicked_unit_id"), body.get("source_item_id"))
                self._json({"ok": True})
            elif method == "PUT" and path == "/api/settings/llm":
                self._json(state.update_settings(body))
            elif method == "POST" and path == "/api/settings/llm/test":
                state.update_settings(body)
                client = state.client()
                if not client:
                    raise ValueError("请完整填写服务地址、模型名和 API Key")
                self._json(client.test())
            else:
                self._error(404, "not found")
        finally:
            db.close()


def find_port(host: str, preferred: int) -> int:
    for port in range(preferred, preferred + 50):
        try:
            probe = ThreadingHTTPServer((host, port), BaseHTTPRequestHandler)
            probe.server_close()
            return port
        except OSError:
            continue
    raise RuntimeError("没有找到可用的本地端口")


def make_server(workspace: Path, port: int = 8765) -> AppServer:
    state = AppState(workspace)
    selected = find_port("127.0.0.1", port)
    return AppServer(("127.0.0.1", selected), Handler, state)


def serve(workspace: Path, port: int = 8765, open_browser: bool = True) -> None:
    server = make_server(workspace, port)
    server.state.worker.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"今天你收了吗：{url}")
    print(f"工作目录：{server.state.workspace}")
    if open_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.state.worker.stop()
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    serve(Path(args.workspace), args.port, not args.no_open)


if __name__ == "__main__":
    main()
