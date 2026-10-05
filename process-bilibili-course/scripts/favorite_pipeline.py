#!/usr/bin/env python3
"""Saved-video import, review, curation, migration, and background jobs."""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import os
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
    schedule_model_retry, update_job, upsert_source,
)
from llm_client import OpenAICompatibleClient, curate_with_ai, triage_with_ai


Progress = Callable[[str, int, str], None]
SourceUrlSink = Callable[[int, str], None]
MODEL_RETRY_DELAYS = (120, 600, 1800)


def _is_temporary_model_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in (
        "408", "429", "500", "502", "503", "504", "service unavailable",
        "temporarily unavailable", "timed out", "timeout", "connection error",
    ))


def _is_model_rate_limited(exc: Exception) -> bool:
    message = str(exc).lower()
    return "429" in message or "too many requests" in message or "resource_exhausted" in message


def _seconds_from_timestamp(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        parts = [float(part) for part in str(value).split(":")]
    except ValueError:
        return None
    if not parts:
        return None
    total = 0.0
    for part in parts:
        total = total * 60 + part
    return max(0.0, total)


def units_from_video_outline(outline: object, *, keywords: list[str] | None = None,
                             topic_tags: list[str] | None = None) -> list[dict]:
    """Turn the approved outline into nodes without sending the transcript again."""
    if not isinstance(outline, dict):
        return []
    overview = str(outline.get("overview", "")).strip()
    units = []
    for section in outline.get("sections", []) if isinstance(outline.get("sections"), list) else []:
        if not isinstance(section, dict):
            continue
        title = str(section.get("title", "")).strip()
        summary = str(section.get("summary", "")).strip()
        if not title or not summary:
            continue
        evidence = section.get("evidence", []) if isinstance(section.get("evidence"), list) else []
        first_evidence = next((row for row in evidence if isinstance(row, dict)), {})
        points = [str(point).strip() for point in section.get("points", []) if str(point).strip()]
        units.append({
            "title": title,
            "method_type": "视频知识节点",
            "when_to_use": overview,
            "steps": [summary, *points],
            "constraints": [], "common_questions": [], "common_symptoms": [],
            "keywords": keywords or [], "topic_tags": topic_tags or [], "status": "approved",
            "segment_start": _seconds_from_timestamp(first_evidence.get("time")),
            "segment_end": None,
        })
    return units


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
    return [canonical for _, canonical in extract_url_pairs(text)]


def extract_url_pairs(text: str) -> list[tuple[str, str]]:
    pairs, seen = [], set()
    for original in re.findall(r"https?://[^\s<>]+", text):
        canonical = canonicalize(original)
        if canonical not in seen:
            seen.add(canonical)
            pairs.append((original.rstrip(".,;!?）)】"), canonical))
    return pairs


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


def _knowledge_score(value: str) -> int:
    action_hits = len(re.findall(
        r"不要|需要|应该|建议|推荐|记得|提前|设置|记录|跟踪|使用|采用|重点|核心|策略|步骤|方法|改|准备|总结|提炼|证明",
        value,
    ))
    noise_hits = len(re.findall(r"今天|这期|下一期|收藏|关注|一定能|我说真的|别慌|很正常|不会差", value))
    question_hits = len(re.findall(r"应该怎么|如何|什么|是否|能否|为什么", value))
    concrete = bool(re.search(r"\d|第一|第二|第三|例如|比如|包括|分为|数据|结果", value))
    return action_hits * 3 + int(concrete) * 2 + int(10 <= len(value) <= 70) - noise_hits * 4 - question_hits * 4


def _select_knowledge_rows(rows: list[tuple[str, str]], count: int = 3) -> list[tuple[str, str]]:
    candidates = [(index, time, text.strip(" ，。；;,.!?！？"), _knowledge_score(text))
                  for index, (time, text) in enumerate(rows) if len(text.strip()) >= 6]
    candidates.sort(key=lambda row: row[3], reverse=True)
    selected: list[tuple[int, str, str]] = []
    for index, time, text, score in candidates:
        if score <= 0:
            continue
        if any(difflib.SequenceMatcher(None, text, existing).ratio() >= 0.62 for _, _, existing in selected):
            continue
        selected.append((index, time, text))
        if len(selected) == count:
            break
    return [(time, text) for _, time, text in sorted(selected[:count])]


def likely_duplicate(source: dict, candidate: dict) -> bool:
    current_title = re.sub(r"\W+", "", str(source.get("title") or "")).lower()
    candidate_title = re.sub(r"\W+", "", str(candidate.get("title") or "")).lower()
    if min(len(current_title), len(candidate_title)) < 8:
        return False
    current_base = str(source.get("canonical_url") or "").split("#", 1)[0]
    candidate_base = str(candidate.get("canonical_url") or "").split("#", 1)[0]
    if current_base and current_base == candidate_base:
        return False
    similarity = difflib.SequenceMatcher(None, current_title, candidate_title).ratio()
    return similarity >= 0.8 or (
        min(len(current_title), len(candidate_title)) >= 12
        and (current_title in candidate_title or candidate_title in current_title)
    )


def make_basic_triage(text: str, timestamped: str = "", title: str = "") -> dict:
    clean = re.sub(r"\s+", " ", text).strip()
    timed_rows = re.findall(r"\[((?:\d{1,2}:)?\d{2}:\d{2})(?:\.\d{1,3})?\]\s*([^\n]+)", timestamped)
    rows = timed_rows or [
        ("", part.strip(" ，。；;,.!?！？"))
        for part in re.split(r"[。！？!?；;\n]", clean) if len(part.strip()) >= 6
    ]
    selected_rows = _select_knowledge_rows(rows, 3)
    selected = [text for _, text in selected_rows]
    summary = "①仅定位原文线索；②未生成问答；③不可直接入库"
    points = ["仅定位原文线索", "未生成问答", "不可直接入库"]
    evidence = [
        {"clue_index": index, "time": time, "excerpt": excerpt[:220]}
        for index, (time, excerpt) in enumerate(selected_rows, 1)
    ]
    keywords = re.findall(r"[A-Za-z][A-Za-z0-9_.+#-]{1,}|[\u4e00-\u9fff]{2,6}", " ".join(selected))
    return {
        "summary_50": summary,
        "point_1": points[0], "point_2": points[1], "point_3": points[2],
        "key_questions": [],
        "keywords": list(dict.fromkeys(keywords))[:12],
        "topic_candidates": [title] if title else [],
        "source_signals": ["full_transcript", "basic"],
        "possible_duplicates": [], "new_points": [],
        "usable_content_start": next((row["time"] for row in evidence if row["time"]), None),
        "evidence": evidence, "ai_mode": "basic",
    }


def import_text(db, text: str, *, saved_at: str | None = None, transcribe: bool = False,
                source_url_sink: SourceUrlSink | None = None) -> dict:
    pairs = extract_url_pairs(text)
    if not pairs:
        raise ValueError("没有找到可导入的视频链接")
    source_ids, job_ids = [], []
    for original_url, url in pairs:
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
                if source_url_sink and original_url != url:
                    source_url_sink(source_id, original_url)
                job_ids.append(create_job(db, source_id, "transcribe"))
    return {"source_ids": source_ids, "job_ids": job_ids}


def transcribe_source(db, workspace: Path, source_id: int, progress: Progress | None = None,
                      source_url: str | None = None) -> None:
    source = get_source(db, source_id)
    if not source:
        raise ValueError(f"source not found: {source_id}")
    if source["platform"] not in {"bilibili", "xiaohongshu"}:
        raise ValueError(f"unsupported public platform: {source['platform']}")
    progress = progress or (lambda *_: None)
    progress("download", 10, "正在读取公开视频信息并下载音频")
    course_name = f"收藏-{source_id}"
    script = Path(__file__).with_name("video_pipeline.py")
    input_url = source_url or source["canonical_url"]
    command = [
        sys.executable, str(script), "run", "--url", "-",
        "--workspace", str(workspace), "--course-name", course_name,
    ]
    page_match = re.search(r"[?&]p=(\d+)", source["canonical_url"])
    if source["platform"] == "bilibili" and page_match:
        command.extend(["--start", page_match.group(1), "--end", page_match.group(1)])
    progress("transcribe", 35, "正在本地转写，较长视频需要一些时间")
    child_env = dict(os.environ, PYTHONIOENCODING="utf-8")
    completed = subprocess.run(
        command, input=input_url, cwd=str(workspace), text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=child_env,
    )
    if completed.returncode:
        lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        detail = " | ".join(lines[-3:])[:700]
        if source["platform"] == "xiaohongshu" and not source_url:
            detail += "；当前进程没有完整分享参数，请重新粘贴原分享链接"
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
    related = [row for row in search(db, source.get("title", ""), 12)
               if row.get("kind") == "source" and row.get("id") != source_id
               and likely_duplicate(source, row)]
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
    detail = source_detail(db, source_id) or {}
    outline_units = units_from_video_outline(
        detail.get("video_outline"), keywords=detail.get("keywords", []),
        topic_tags=detail.get("topic_candidates", []),
    )
    if manual_units is None and outline_units:
        progress = progress or (lambda *_: None)
        progress("curate", 45, "正在根据已审核的视频结构生成知识节点")
        units = outline_units
    elif manual_units is None:
        # A basic triage has no approved structure. Do not send the full
        # transcript to a model a second time just because the source was approved.
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
    else:
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
    progress("curate", 100, f"已生成 {len(unit_ids)} 个知识节点")
    return {"source_id": source_id, "status": "curated", "unit_ids": unit_ids, "note": str(path)}


def process_job(db_path: Path, workspace: Path, job: dict,
                client: OpenAICompatibleClient | None = None,
                source_url: str | None = None) -> None:
    db = connect(db_path)
    job_id, source_id, job_type = job["id"], job["source_item_id"], job["job_type"]

    def progress(phase: str, percent: int, message: str) -> None:
        update_job(db, job_id, phase=phase, progress=max(0, min(100, percent)), message=message)

    try:
        if job_type == "transcribe":
            transcribe_source(db, workspace, source_id, progress, source_url)
            # Audio and transcript are already durable at this point. Keep the
            # cloud-only structure extraction in its own resumable job.
            triage_job = create_job(db, source_id, "triage")
            update_job(
                db, job_id, status="succeeded", phase="transcribed", progress=100,
                message=f"本地转写完成，已创建结构提取任务 #{triage_job}",
            )
            return
        elif job_type == "triage":
            triage_source(db, workspace, source_id, client, progress)
        elif job_type == "curate":
            curate_source(db, workspace, source_id, client, progress=progress)
        else:
            raise ValueError(f"unsupported job type: {job_type}")
        update_job(db, job_id, status="succeeded", phase="complete", progress=100, message="处理完成")
    except Exception as exc:
        attempt_count = int(job.get("attempt_count") or 0) + 1
        source = get_source(db, source_id)
        has_transcript = bool(source and transcript_text(source, workspace))
        if job_type == "triage" and has_transcript and _is_model_rate_limited(exc):
            # The local transcript is already complete. Keep the source reviewable
            # instead of treating a temporary cloud quota limit as a lost video.
            try:
                triage_source(db, workspace, source_id, None, progress)
            except Exception as fallback_exc:
                update_job(
                    db, job_id, status="failed", phase="failed", error=str(fallback_exc),
                    message="基础提取失败，可重试",
                )
                return
            update_job(
                db, job_id, status="succeeded", phase="basic_triage", progress=100,
                message="模型额度受限，已生成基础原文线索，可继续人工审核",
            )
            return
        if job_type in {"triage", "curate"} and _is_temporary_model_error(exc) and attempt_count <= len(MODEL_RETRY_DELAYS):
            schedule_model_retry(
                db, job_id, delay_seconds=MODEL_RETRY_DELAYS[attempt_count - 1],
                attempt_count=attempt_count, error=str(exc),
            )
            return
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
        self.source_urls: dict[int, str] = {}
        self.source_urls_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="saved-video-worker", daemon=True)

    def start(self) -> None:
        db = connect(self.db_path)
        requeue_interrupted_jobs(db)
        db.close()
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def register_source_url(self, source_id: int, url: str) -> None:
        with self.source_urls_lock:
            self.source_urls[source_id] = url

    def source_url(self, source_id: int) -> str | None:
        with self.source_urls_lock:
            return self.source_urls.get(source_id)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            db = connect(self.db_path)
            job = next_job(db)
            db.close()
            if job:
                try:
                    process_job(
                        self.db_path, self.workspace, job, self.client_getter(),
                        self.source_url(job["source_item_id"]),
                    )
                except Exception as exc:
                    # A worker must never die and leave a job permanently marked
                    # as running, even if an unexpected fallback path fails.
                    failed_db = connect(self.db_path)
                    try:
                        update_job(
                            failed_db, job["id"], status="failed", phase="failed", error=str(exc),
                            message="处理失败，可重试",
                        )
                    finally:
                        failed_db.close()
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
