#!/usr/bin/env python3
"""Saved-video import, review, curation, migration, and background jobs."""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from knowledge_db import (
    add_triage, add_unit, connect, create_job, dashboard_counts, get_job, get_source,
    index_source, link_unit_source, list_jobs, log_search, next_job, record_search_feedback,
    requeue_interrupted_jobs, retry_job, review_queue, search, set_status, source_detail,
    update_job, upsert_source,
)
from llm_client import OpenAICompatibleClient, compact_summary, curate_with_ai, triage_with_ai


Progress = Callable[[str, int, str], None]


def platform_for(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    if "bilibili.com" in host or host.endswith("b23.tv"):
        return "bilibili"
    if "xiaohongshu.com" in host or host.endswith("xhslink.com"):
        return "xiaohongshu"
    return "unknown"


def canonicalize(value: str) -> str:
    match = re.search(r"https?://[^\s<>]+", value.strip())
    url = (match.group(0) if match else value.strip()).rstrip(".,;!?）)】")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("请输入有效的 http 或 https 视频链接")
    keep = [(key, item) for key, item in parse_qsl(parts.query, keep_blank_values=True) if key == "p"]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), urlencode(keep), ""))


def item_id(url: str) -> str:
    match = re.search(r"(BV[0-9A-Za-z]{10}|[0-9A-Fa-f]{16,})", url)
    return match.group(1) if match else canonicalize(url)


def extract_urls(text: str) -> list[str]:
    urls = re.findall(r"https?://[^\s<>]+", text)
    return list(dict.fromkeys(canonicalize(url) for url in urls))


def read_inputs(urls: list[str], input_path: str | None) -> list[dict]:
    rows = [{"url": value} for value in urls]
    if not input_path:
        return rows
    path = Path(input_path)
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows.extend(dict(row) for row in csv.DictReader(handle) if row.get("url"))
    else:
        rows.extend({"url": line.strip()} for line in path.read_text(encoding="utf-8-sig").splitlines()
                    if line.strip() and not line.lstrip().startswith("#"))
    return rows


def _resolved_path(value: str | None, workspace: Path) -> Path | None:
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else workspace / path


def transcript_text(source: dict, workspace: Path) -> str:
    path = _resolved_path(source.get("transcript_path"), workspace)
    return path.read_text(encoding="utf-8-sig") if path and path.exists() else ""


def timestamped_text(source: dict, workspace: Path) -> str:
    path = _resolved_path(source.get("timestamped_transcript_path"), workspace)
    return path.read_text(encoding="utf-8-sig") if path and path.exists() else ""


def _sample_evenly(values: list[str], count: int = 3) -> list[str]:
    unique = list(dict.fromkeys(value for value in values if value))
    if len(unique) <= count:
        return unique
    positions = [round(index * (len(unique) - 1) / (count - 1)) for index in range(count)]
    return [unique[position] for position in positions]


def make_basic_triage(text: str, timestamped: str = "", title: str = "") -> dict:
    clean = re.sub(r"\s+", " ", text).strip()
    sentences = [part.strip(" ，。；;,.!?！？") for part in re.split(r"[。！？!?；;\n]", clean)
                 if len(part.strip()) >= 6]
    selected = _sample_evenly(sentences, 3)
    summary, points = compact_summary(selected)
    timed_lines = re.findall(r"\[((?:\d{1,2}:)?\d{2}:\d{2})\]\s*([^\n]+)", timestamped)
    evidence_rows = _sample_evenly([f"{time}\t{line}" for time, line in timed_lines], 3)
    evidence = []
    for index, row in enumerate(evidence_rows):
        time, excerpt = row.split("\t", 1)
        evidence.append({"point": points[min(index, 2)], "time": time, "excerpt": excerpt[:120]})
    keywords = re.findall(r"[A-Za-z][A-Za-z0-9_.+#-]{1,}|[\u4e00-\u9fff]{2,6}", clean)
    return {
        "summary_50": summary,
        "point_1": points[0], "point_2": points[1], "point_3": points[2],
        "keywords": list(dict.fromkeys(keywords))[:12],
        "topic_candidates": [title] if title else [],
        "source_signals": ["full_transcript", "basic"],
        "possible_duplicates": [], "new_points": [],
        "usable_content_start": evidence[0]["time"] if evidence else None,
        "evidence": evidence, "ai_mode": "basic",
    }


def import_text(db, text: str, *, saved_at: str | None = None, transcribe: bool = False) -> dict:
    urls = extract_urls(text)
    if not urls:
        raise ValueError("没有找到可导入的视频链接")
    source_ids, job_ids = [], []
    for url in urls:
        platform = platform_for(url)
        if platform == "unknown":
            raise ValueError(f"暂不支持这个公开视频平台：{url}")
        source_id = upsert_source(db, {
            "platform": platform, "canonical_url": url, "platform_item_id": item_id(url),
            "saved_at": saved_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        })
        source_ids.append(source_id)
        if transcribe:
            source = get_source(db, source_id)
            if not transcript_text(source, Path(".")):
                job_ids.append(create_job(db, source_id, "transcribe"))
    return {"source_ids": source_ids, "job_ids": job_ids}


def transcribe_source(db, workspace: Path, source_id: int, progress: Progress | None = None) -> None:
    source = get_source(db, source_id)
    if not source:
        raise ValueError(f"source not found: {source_id}")
    if source["platform"] not in {"bilibili", "xiaohongshu"}:
        raise ValueError(f"unsupported public platform: {source['platform']}")
    progress = progress or (lambda *_: None)
    progress("download", 10, "正在读取公开视频信息并下载音频")
    course_name = f"收藏-{source_id}"
    script = Path(__file__).with_name("video_pipeline.py")
    command = [
        sys.executable, str(script), "run", "--url", source["canonical_url"],
        "--workspace", str(workspace), "--course-name", course_name,
    ]
    page_match = re.search(r"[?&]p=(\d+)", source["canonical_url"])
    if source["platform"] == "bilibili" and page_match:
        command.extend(["--start", page_match.group(1), "--end", page_match.group(1)])
    progress("transcribe", 35, "正在本地转写，较长视频需要一些时间")
    completed = subprocess.run(
        command, cwd=str(workspace), text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    if completed.returncode:
        lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        detail = " | ".join(lines[-3:])[:700]
        raise RuntimeError(f"公开视频处理失败（退出码 {completed.returncode}）：{detail}")
    manifest_path = workspace / "后台处理文件" / course_name / "course-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    episode = next((row for row in manifest.get("episodes", [])
                    if row.get("state") in {"transcribed", "notes_done", "validated"}), None)
    if not episode:
        raise RuntimeError("pipeline completed without a transcribed episode")
    number = int(episode["page"])
    course_root = workspace / "课程资料" / course_name
    work_root = workspace / "后台处理文件" / course_name
    transcript = course_root / "02-逐字稿" / f"{number:02d}-transcript.txt"
    timestamped = work_root / "时间戳逐字稿" / f"{number:02d}-transcript-timestamped.md"
    metadata = work_root / "元数据" / f"{number:02d}-metadata.json"
    if not transcript.exists():
        raise RuntimeError("transcript file is missing after pipeline completion")
    db.execute("""
        UPDATE source_items SET title=COALESCE(NULLIF(title,''),?), duration_seconds=?,
            transcript_path=?, timestamped_transcript_path=?, metadata_path=?,
            status='transcribed', error_message=NULL WHERE id=?
    """, (episode.get("title", ""), episode.get("duration"), str(transcript), str(timestamped),
          str(metadata), source_id))
    db.commit()
    index_source(db, source_id, transcript.read_text(encoding="utf-8-sig"))
    progress("transcribe", 70, "逐字稿已完成")


def triage_source(db, workspace: Path, source_id: int,
                  client: OpenAICompatibleClient | None = None,
                  progress: Progress | None = None) -> dict:
    source = get_source(db, source_id)
    if not source:
        raise ValueError(f"source not found: {source_id}")
    text = transcript_text(source, workspace)
    if not text:
        raise ValueError("生成速览前需要完整逐字稿")
    progress = progress or (lambda *_: None)
    progress("triage", 75, "正在阅读完整逐字稿并生成三点速览")
    data = triage_with_ai(client, timestamped_text(source, workspace) or text, source.get("title", "")) \
        if client else make_basic_triage(text, timestamped_text(source, workspace), source.get("title", ""))
    related = [row for row in search(db, " ".join(data.get("keywords", [])[:3]), 6)
               if row.get("kind") == "source" and row.get("id") != source_id]
    data["possible_duplicates"] = [
        {"source_id": row["id"], "title": row.get("title", ""), "url": row.get("canonical_url", "")}
        for row in related[:3]
    ]
    add_triage(db, source_id, data)
    progress("triage", 95, "三点速览已生成，等待人工审核")
    return data


def review_source(db, source_id: int, decision: str) -> dict:
    status = {"approve": "approved", "defer": "deferred", "reject": "rejected"}.get(decision)
    if not status:
        raise ValueError("decision must be approve, defer, or reject")
    source = get_source(db, source_id)
    if not source:
        raise ValueError(f"source not found: {source_id}")
    if decision == "approve" and source["status"] not in {"triage_ready", "deferred", "legacy_imported", "approved"}:
        raise ValueError("只有待审核、稍后处理或旧资料可以批准")
    set_status(db, source_id, status, decision)
    return {"source_id": source_id, "status": status}


def _write_curated_note(workspace: Path, source: dict, units: list[dict]) -> Path:
    target = workspace / "知识库" / "curated"
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{source['id']}-knowledge.md"
    lines = [
        f"# {source.get('title') or '收藏内容'}", "", f"- 来源：{source['canonical_url']}",
        f"- 作者：{source.get('author_name') or '未知'}", "",
    ]
    if not units:
        lines.extend(["## 整理结果", "", "未发现可独立沉淀的方法。原始逐字稿仍保留。", ""])
    for unit in units:
        lines.extend([
            f"## {unit['title']}", "", f"**适用场景：** {unit.get('when_to_use') or '未明确'}", "",
            "**步骤**", "", *[f"{index}. {step}" for index, step in enumerate(unit.get("steps", []), 1)], "",
            "**限制**", "", *[f"- {item}" for item in unit.get("constraints", [])], "",
        ])
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def curate_source(db, workspace: Path, source_id: int,
                  client: OpenAICompatibleClient | None = None,
                  manual_units: list[dict] | None = None,
                  progress: Progress | None = None) -> dict:
    source = get_source(db, source_id)
    if not source:
        raise ValueError(f"source not found: {source_id}")
    if source["status"] not in {"approved", "curated"}:
        raise ValueError("deep curation requires explicit approval first")
    text = transcript_text(source, workspace)
    if not text:
        raise ValueError("curation requires a complete transcript")
    if client is None and manual_units is None:
        detail = source_detail(db, source_id) or {}
        return {
            "manual_required": True,
            "prefill": {
                "title": detail.get("point_1") or source.get("title") or "新方法",
                "when_to_use": detail.get("point_2", ""),
                "steps": [value for value in (detail.get("point_1"), detail.get("point_2"), detail.get("point_3")) if value],
                "constraints": [], "common_questions": [], "common_symptoms": [],
                "keywords": detail.get("keywords", []), "topic_tags": [],
            },
        }
    progress = progress or (lambda *_: None)
    progress("curate", 20, "正在提取可复用的方法")
    units = manual_units if manual_units is not None else curate_with_ai(
        client, timestamped_text(source, workspace) or text, source.get("title", "")
    )
    unit_ids = []
    for unit in units:
        unit_id = add_unit(db, unit)
        link_unit_source(
            db, unit_id, source_id, segment_start=unit.get("segment_start"),
            segment_end=unit.get("segment_end"), contribution_type="primary",
        )
        unit_ids.append(unit_id)
    set_status(db, source_id, "curated", "curated")
    path = _write_curated_note(workspace, source, units)
    progress("curate", 100, f"已生成 {len(unit_ids)} 张方法卡")
    return {"source_id": source_id, "status": "curated", "unit_ids": unit_ids, "note": str(path)}


def process_job(db_path: Path, workspace: Path, job: dict,
                client: OpenAICompatibleClient | None = None) -> None:
    db = connect(db_path)
    job_id, source_id, job_type = job["id"], job["source_item_id"], job["job_type"]

    def progress(phase: str, percent: int, message: str) -> None:
        update_job(db, job_id, phase=phase, progress=max(0, min(100, percent)), message=message)

    try:
        if job_type == "transcribe":
            transcribe_source(db, workspace, source_id, progress)
            triage_source(db, workspace, source_id, client, progress)
        elif job_type == "triage":
            triage_source(db, workspace, source_id, client, progress)
        elif job_type == "curate":
            curate_source(db, workspace, source_id, client, progress=progress)
        else:
            raise ValueError(f"unsupported job type: {job_type}")
        update_job(db, job_id, status="succeeded", phase="complete", progress=100, message="处理完成")
    except Exception as exc:
        if job_type in {"transcribe", "triage"}:
            db.execute("UPDATE source_items SET status='error', error_message=? WHERE id=?", (str(exc), source_id))
            db.commit()
        update_job(db, job_id, status="failed", phase="failed", error=str(exc), message="处理失败，可重试")
    finally:
        db.close()


class JobWorker:
    def __init__(self, db_path: Path, workspace: Path,
                 client_getter: Callable[[], OpenAICompatibleClient | None]):
        self.db_path = db_path
        self.workspace = workspace
        self.client_getter = client_getter
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="saved-video-worker", daemon=True)

    def start(self) -> None:
        db = connect(self.db_path)
        requeue_interrupted_jobs(db)
        db.close()
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def _run(self) -> None:
        while not self.stop_event.is_set():
            db = connect(self.db_path)
            job = next_job(db)
            db.close()
            if job:
                process_job(self.db_path, self.workspace, job, self.client_getter())
            else:
                self.stop_event.wait(1)


def migrate_existing(workspace: Path) -> dict:
    db = connect(workspace / "知识库" / "knowledge.db")
    migrated = 0
    for manifest_path in (workspace / "后台处理文件").glob("*/course-manifest.json"):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        course = manifest.get("course", manifest_path.parent.name)
        course_root = workspace / "课程资料" / course
        for episode in manifest.get("episodes", []):
            page = int(episode["page"])
            base_url = manifest.get("source_url", manifest.get("resolved_url", ""))
            canonical = f"{base_url}#p={page}"
            transcript = course_root / "02-逐字稿" / f"{page:02d}-transcript.txt"
            timestamped = manifest_path.parent / "时间戳逐字稿" / f"{page:02d}-transcript-timestamped.md"
            metadata = manifest_path.parent / "元数据" / f"{page:02d}-metadata.json"
            source_id = upsert_source(db, {
                "platform": manifest.get("platform", "bilibili"), "canonical_url": canonical,
                "platform_item_id": manifest.get("source_id", ""),
                "title": f"{course}：{episode.get('title', '')}",
                "imported_at": datetime.now(timezone.utc).isoformat(),
                "transcript_path": str(transcript), "timestamped_transcript_path": str(timestamped),
                "metadata_path": str(metadata), "status": "legacy_imported",
                "review_decision": "legacy_migration",
            })
            index_source(db, source_id, transcript.read_text(encoding="utf-8-sig") if transcript.exists() else "")
            migrated += 1
    result = {"migrated_sources": migrated, "database": str(workspace / "知识库" / "knowledge.db")}
    db.close()
    return result


def command_import(args) -> None:
    workspace = Path(args.workspace).resolve()
    db = connect(workspace / "知识库" / "knowledge.db")
    text = "\n".join(row["url"] for row in read_inputs(args.url, args.input))
    print(json.dumps(import_text(db, text, saved_at=args.saved_at, transcribe=args.transcribe), ensure_ascii=False, indent=2))


def command_triage(args) -> None:
    workspace = Path(args.workspace).resolve()
    db = connect(workspace / "知识库" / "knowledge.db")
    print(json.dumps(triage_source(db, workspace, args.source_id), ensure_ascii=False, indent=2))


def command_queue(args) -> None:
    db = connect(Path(args.workspace).resolve() / "知识库" / "knowledge.db")
    print(json.dumps(review_queue(db, now=args.now, limit=args.limit), ensure_ascii=False, indent=2))


def command_review(args) -> None:
    db = connect(Path(args.workspace).resolve() / "知识库" / "knowledge.db")
    print(json.dumps(review_source(db, args.source_id, args.decision), ensure_ascii=False))


def command_curate(args) -> None:
    workspace = Path(args.workspace).resolve()
    db = connect(workspace / "知识库" / "knowledge.db")
    manual = json.loads(Path(args.json).read_text(encoding="utf-8-sig")) if args.json else None
    if isinstance(manual, dict):
        manual = manual.get("units", [manual])
    print(json.dumps(curate_source(db, workspace, args.source_id, manual_units=manual), ensure_ascii=False, indent=2))


def command_export(args) -> None:
    workspace = Path(args.workspace).resolve()
    db = connect(workspace / "知识库" / "knowledge.db")
    data = review_queue(db, limit=args.limit)
    data["counts"] = dashboard_counts(db)
    export = workspace / "知识库" / "exports"
    export.mkdir(parents=True, exist_ok=True)
    json_path, markdown_path = export / "today_review.json", export / "today_review.md"
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 今日收藏审核", "", f"近 48 小时待审核：{len(data['recent'])}",
             f"历史待审核：{len(data['historical'])}", ""]
    for label, title in (("recent", "近 48 小时"), ("historical", "历史")):
        lines.extend([f"## {title}", ""])
        lines.extend(f"- {row.get('title') or row['canonical_url']}：{row.get('summary_50', '')}" for row in data[label])
        lines.append("")
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"json": str(json_path), "markdown": str(markdown_path)}, ensure_ascii=False, indent=2))


def command_search(args) -> None:
    db = connect(Path(args.workspace).resolve() / "知识库" / "knowledge.db")
    results = search(db, args.query, args.limit)
    event_id = log_search(db, args.query, [row["id"] for row in results if row["kind"] == "knowledge_unit"])
    print(json.dumps({"event_id": event_id, "results": results}, ensure_ascii=False, indent=2))


def command_migrate(args) -> None:
    print(json.dumps(migrate_existing(Path(args.workspace).resolve()), ensure_ascii=False, indent=2))


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=(
        "import", "triage", "queue", "review", "curate", "export-today", "search", "migrate-existing",
    ))
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--url", action="append", default=[])
    parser.add_argument("--input")
    parser.add_argument("--saved-at")
    parser.add_argument("--transcribe", action="store_true")
    parser.add_argument("--source-id", type=int)
    parser.add_argument("--decision", choices=("approve", "defer", "reject"))
    parser.add_argument("--now")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--query")
    parser.add_argument("--json")
    return parser


def main() -> None:
    args = parser().parse_args()
    commands = {
        "import": command_import, "triage": command_triage, "queue": command_queue,
        "review": command_review, "curate": command_curate, "export-today": command_export,
        "search": command_search, "migrate-existing": command_migrate,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
