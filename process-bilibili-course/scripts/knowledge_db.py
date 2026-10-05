#!/usr/bin/env python3
"""Versioned SQLite storage for saved videos and reusable knowledge cards."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SCHEMA_VERSION = 7
SOURCE_STATUSES = {
    "discovered", "transcribed", "triage_ready", "approved", "deferred",
    "rejected", "curated", "legacy_imported", "error",
}
JOB_STATUSES = {"queued", "running", "succeeded", "failed"}

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_items (
    id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,
    canonical_url TEXT NOT NULL UNIQUE,
    platform_item_id TEXT,
    title TEXT NOT NULL DEFAULT '',
    author_name TEXT DEFAULT '',
    author_type TEXT DEFAULT '',
    saved_at TEXT,
    imported_at TEXT NOT NULL,
    duration_seconds INTEGER,
    transcript_path TEXT,
    timestamped_transcript_path TEXT,
    metadata_path TEXT,
    status TEXT NOT NULL DEFAULT 'discovered',
    reviewed_at TEXT,
    review_decision TEXT,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_source_review_queue ON source_items(status, saved_at DESC);
CREATE TABLE IF NOT EXISTS triage (
    source_item_id INTEGER PRIMARY KEY REFERENCES source_items(id) ON DELETE CASCADE,
    summary_50 TEXT NOT NULL,
    point_1 TEXT NOT NULL DEFAULT '',
    point_2 TEXT NOT NULL DEFAULT '',
    point_3 TEXT NOT NULL DEFAULT '',
    keywords TEXT NOT NULL DEFAULT '[]',
    topic_candidates TEXT NOT NULL DEFAULT '[]',
    source_signals TEXT NOT NULL DEFAULT '[]',
    possible_duplicates TEXT NOT NULL DEFAULT '[]',
    new_points TEXT NOT NULL DEFAULT '[]',
    usable_content_start TEXT,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    key_questions_json TEXT NOT NULL DEFAULT '[]',
    video_outline_json TEXT NOT NULL DEFAULT '{}',
    ai_mode TEXT NOT NULL DEFAULT 'basic',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_units (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    method_type TEXT NOT NULL DEFAULT '',
    when_to_use TEXT NOT NULL DEFAULT '',
    steps TEXT NOT NULL DEFAULT '[]',
    constraints TEXT NOT NULL DEFAULT '[]',
    common_questions TEXT NOT NULL DEFAULT '[]',
    common_symptoms TEXT NOT NULL DEFAULT '[]',
    keywords TEXT NOT NULL DEFAULT '[]',
    topic_tags TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS unit_sources (
    knowledge_unit_id INTEGER NOT NULL REFERENCES knowledge_units(id) ON DELETE CASCADE,
    source_item_id INTEGER NOT NULL REFERENCES source_items(id) ON DELETE CASCADE,
    segment_start REAL,
    segment_end REAL,
    contribution_type TEXT NOT NULL DEFAULT '',
    new_points TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY(knowledge_unit_id, source_item_id)
);
CREATE TABLE IF NOT EXISTS knowledge_topics (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    topic_type TEXT NOT NULL DEFAULT '',
    parent_id INTEGER REFERENCES knowledge_topics(id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_topics_parent_position
    ON knowledge_topics(parent_id, position, id);
CREATE TABLE IF NOT EXISTS topic_units (
    topic_id INTEGER NOT NULL REFERENCES knowledge_topics(id) ON DELETE CASCADE,
    knowledge_unit_id INTEGER NOT NULL REFERENCES knowledge_units(id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(topic_id, knowledge_unit_id)
);
CREATE TABLE IF NOT EXISTS search_events (
    id INTEGER PRIMARY KEY,
    query TEXT NOT NULL,
    query_time TEXT NOT NULL,
    returned_unit_ids TEXT NOT NULL DEFAULT '[]',
    clicked_unit_id INTEGER,
    source_item_id INTEGER,
    user_feedback TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS processing_jobs (
    id INTEGER PRIMARY KEY,
    source_item_id INTEGER NOT NULL REFERENCES source_items(id) ON DELETE CASCADE,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    phase TEXT NOT NULL DEFAULT 'queued',
    progress INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error_message TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    retry_after TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON processing_jobs(status, created_at);
CREATE VIRTUAL TABLE IF NOT EXISTS source_fts USING fts5(
    source_item_id UNINDEXED, title, transcript_text
);
CREATE VIRTUAL TABLE IF NOT EXISTS unit_fts USING fts5(
    knowledge_unit_id UNINDEXED, title, when_to_use, steps, constraints,
    common_questions, common_symptoms, keywords, topic_tags
);
CREATE VIRTUAL TABLE IF NOT EXISTS triage_fts USING fts5(
    source_item_id UNINDEXED, question_text, answer_text
);
"""

JSON_FIELDS = {
    "keywords", "topic_candidates", "source_signals", "possible_duplicates",
    "new_points", "evidence_json", "key_questions_json", "video_outline_json", "steps", "constraints", "common_questions",
    "common_symptoms", "topic_tags", "payload", "returned_unit_ids",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def as_json(value: object) -> str:
    return json.dumps(value if value is not None else [], ensure_ascii=False)


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _backup_database_for_upgrade(path: Path) -> Path | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    probe = sqlite3.connect(str(path))
    try:
        if not _table_exists(probe, "source_items"):
            return None
        if _table_exists(probe, "schema_meta"):
            row = probe.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            version = int(row[0]) if row and str(row[0]).isdigit() else 0
        else:
            version = 0
    finally:
        probe.close()
    if version >= SCHEMA_VERSION:
        return None
    backup_dir = path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"{path.stem}-before-v{SCHEMA_VERSION}-{stamp}{path.suffix}"
    shutil.copy2(path, backup)
    return backup


def _ensure_column(db: sqlite3.Connection, table: str, definition: str) -> None:
    name = definition.split()[0]
    columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    if name not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def _migrate(db: sqlite3.Connection) -> None:
    _ensure_column(db, "triage", "evidence_json TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(db, "triage", "key_questions_json TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(db, "triage", "video_outline_json TEXT NOT NULL DEFAULT '{}'")
    _ensure_column(db, "triage", "ai_mode TEXT NOT NULL DEFAULT 'basic'")
    _ensure_column(db, "processing_jobs", "attempt_count INTEGER NOT NULL DEFAULT 0")
    _ensure_column(db, "processing_jobs", "retry_after TEXT")
    _ensure_column(db, "knowledge_topics", "topic_type TEXT NOT NULL DEFAULT ''")
    db.execute("""
        UPDATE source_items
        SET status='legacy_imported', reviewed_at=NULL, review_decision='legacy_migration'
        WHERE status='curated' AND review_decision='legacy_curated'
    """)
    db.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    db.commit()


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _backup_database_for_upgrade(path)
    db = sqlite3.connect(str(path), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    _migrate(db)
    return db


def _decoded(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    value = dict(row)
    for key in JSON_FIELDS & value.keys():
        if isinstance(value[key], str):
            try:
                value[key] = json.loads(value[key])
            except json.JSONDecodeError:
                value[key] = []
    return value


def upsert_source(db: sqlite3.Connection, item: dict) -> int:
    missing = {"platform", "canonical_url"} - set(item)
    if missing:
        raise ValueError(f"missing source fields: {', '.join(sorted(missing))}")
    status = item.get("status", "discovered")
    if status not in SOURCE_STATUSES:
        raise ValueError(f"invalid source status: {status}")
    values = (
        item["platform"], item["canonical_url"], item.get("platform_item_id", ""),
        item.get("title", ""), item.get("author_name", ""), item.get("author_type", ""),
        item.get("saved_at"), item.get("imported_at") or now_iso(), item.get("duration_seconds"),
        item.get("transcript_path"), item.get("timestamped_transcript_path"), item.get("metadata_path"),
        status, item.get("reviewed_at"), item.get("review_decision"), item.get("error_message"),
    )
    db.execute("""
        INSERT INTO source_items(platform, canonical_url, platform_item_id, title, author_name, author_type,
            saved_at, imported_at, duration_seconds, transcript_path, timestamped_transcript_path, metadata_path,
            status, reviewed_at, review_decision, error_message)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(canonical_url) DO UPDATE SET
            title=COALESCE(NULLIF(excluded.title,''), source_items.title),
            author_name=COALESCE(NULLIF(excluded.author_name,''), source_items.author_name),
            author_type=COALESCE(NULLIF(excluded.author_type,''), source_items.author_type),
            saved_at=COALESCE(excluded.saved_at, source_items.saved_at),
            duration_seconds=COALESCE(excluded.duration_seconds, source_items.duration_seconds),
            transcript_path=COALESCE(excluded.transcript_path, source_items.transcript_path),
            timestamped_transcript_path=COALESCE(excluded.timestamped_transcript_path, source_items.timestamped_transcript_path),
            metadata_path=COALESCE(excluded.metadata_path, source_items.metadata_path)
    """, values)
    source_id = int(db.execute("SELECT id FROM source_items WHERE canonical_url=?", (item["canonical_url"],)).fetchone()[0])
    db.commit()
    return source_id


def get_source(db: sqlite3.Connection, source_id: int) -> dict | None:
    return _decoded(db.execute("SELECT * FROM source_items WHERE id=?", (source_id,)).fetchone())


def set_status(db: sqlite3.Connection, source_id: int, status: str, decision: str | None = None) -> None:
    if status not in SOURCE_STATUSES:
        raise ValueError(f"invalid source status: {status}")
    reviewed_at = now_iso() if status in {"approved", "deferred", "rejected"} else None
    db.execute("""
        UPDATE source_items
        SET status=?, reviewed_at=COALESCE(?, reviewed_at),
            review_decision=COALESCE(?, review_decision), error_message=NULL
        WHERE id=?
    """, (status, reviewed_at, decision, source_id))
    db.commit()


def list_sources(db: sqlite3.Connection, status: str | None = None, limit: int = 100, offset: int = 0) -> list[dict]:
    where, params = "", []
    if status:
        where, params = "WHERE s.status=?", [status]
    rows = db.execute(f"""
        SELECT s.*, t.summary_50, t.point_1, t.point_2, t.point_3, t.ai_mode,
               t.evidence_json, t.possible_duplicates, t.keywords
        FROM source_items s LEFT JOIN triage t ON t.source_item_id=s.id
        {where}
        ORDER BY COALESCE(s.saved_at, s.imported_at) DESC LIMIT ? OFFSET ?
    """, (*params, limit, offset)).fetchall()
    return [_decoded(row) for row in rows]


def source_detail(db: sqlite3.Connection, source_id: int) -> dict | None:
    source = _decoded(db.execute("""
        SELECT s.*, t.summary_50, t.point_1, t.point_2, t.point_3, t.keywords,
               t.topic_candidates, t.source_signals, t.possible_duplicates,
               t.new_points, t.usable_content_start, t.evidence_json, t.key_questions_json,
               t.video_outline_json, t.ai_mode
        FROM source_items s LEFT JOIN triage t ON t.source_item_id=s.id
        WHERE s.id=?
    """, (source_id,)).fetchone())
    if not source:
        return None
    source["source_url_at"] = _source_url_at(
        source.get("canonical_url", ""), source.get("usable_content_start")
    )
    source["key_questions"] = source.get("key_questions_json") or []
    for question in source["key_questions"]:
        for evidence in question.get("evidence", []):
            evidence["source_url_at"] = _source_url_at(
                source.get("canonical_url", ""), evidence.get("time")
            )
    source["video_outline"] = source.get("video_outline_json") or {}
    for section in source["video_outline"].get("sections", []):
        if not isinstance(section, dict):
            continue
        for evidence in section.get("evidence", []):
            if isinstance(evidence, dict):
                evidence["source_url_at"] = _source_url_at(
                    source.get("canonical_url", ""), evidence.get("time")
                )
    source["basic_clues"] = []
    for evidence in source.get("evidence_json") or []:
        if not isinstance(evidence, dict) or not evidence.get("excerpt"):
            continue
        source["basic_clues"].append({
            "time": evidence.get("time", ""),
            "excerpt": evidence["excerpt"],
            "source_url_at": _source_url_at(source.get("canonical_url", ""), evidence.get("time")),
        })
    source["units"] = [_decoded(row) for row in db.execute("""
        SELECT u.*, us.segment_start, us.segment_end, us.contribution_type
        FROM unit_sources us JOIN knowledge_units u ON u.id=us.knowledge_unit_id
        WHERE us.source_item_id=? ORDER BY u.updated_at DESC
    """, (source_id,)).fetchall()]
    return source


def dashboard_counts(db: sqlite3.Connection) -> dict:
    statuses = {row["status"]: row["count"] for row in db.execute(
        "SELECT status, COUNT(*) AS count FROM source_items GROUP BY status"
    )}
    return {
        "sources": sum(statuses.values()),
        "pending_review": statuses.get("triage_ready", 0),
        "legacy_imported": statuses.get("legacy_imported", 0),
        "approved": statuses.get("approved", 0),
        "curated": statuses.get("curated", 0),
        "failed": statuses.get("error", 0) + db.execute(
            "SELECT COUNT(*) FROM processing_jobs WHERE status='failed'"
        ).fetchone()[0],
        "knowledge_units": db.execute("SELECT COUNT(*) FROM knowledge_units").fetchone()[0],
        "status_counts": statuses,
    }


def add_triage(db: sqlite3.Connection, source_id: int, data: dict) -> None:
    summary = str(data.get("summary_50", "")).strip()
    if len(summary) > 50:
        raise ValueError("summary_50 must be at most 50 characters")
    questions = _normalize_questions(data.get("key_questions", []))
    outline = _normalize_outline(data.get("video_outline", {}))
    if outline:
        summary = outline["overview"][:50] or summary
        points = [section["title"] for section in outline["sections"]]
        while len(points) < 3:
            points.append("")
        evidence = [
            {"section_index": index, "point": section["title"], **row}
            for index, section in enumerate(outline["sections"], 1)
            for row in section["evidence"]
        ]
    elif questions:
        summary, points, evidence = _legacy_triage_fields(questions)
    else:
        points = [data.get("point_1", ""), data.get("point_2", ""), data.get("point_3", "")]
        evidence = data.get("evidence", [])
    db.execute("""
        INSERT INTO triage(source_item_id, summary_50, point_1, point_2, point_3, keywords,
            topic_candidates, source_signals, possible_duplicates, new_points, usable_content_start,
            evidence_json, key_questions_json, video_outline_json, ai_mode, created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(source_item_id) DO UPDATE SET
            summary_50=excluded.summary_50, point_1=excluded.point_1, point_2=excluded.point_2,
            point_3=excluded.point_3, keywords=excluded.keywords,
            topic_candidates=excluded.topic_candidates, source_signals=excluded.source_signals,
            possible_duplicates=excluded.possible_duplicates, new_points=excluded.new_points,
            usable_content_start=excluded.usable_content_start, evidence_json=excluded.evidence_json,
            key_questions_json=excluded.key_questions_json,
            video_outline_json=excluded.video_outline_json,
            ai_mode=excluded.ai_mode, created_at=excluded.created_at
    """, (
        source_id, summary, points[0], points[1], points[2],
        as_json(data.get("keywords", [])), as_json(data.get("topic_candidates", [])),
        as_json(data.get("source_signals", [])), as_json(data.get("possible_duplicates", [])),
        as_json(data.get("new_points", [])), data.get("usable_content_start"),
        as_json(evidence), as_json(questions), as_json(outline), data.get("ai_mode", "basic"), data.get("created_at") or now_iso(),
    ))
    _index_triage(db, source_id, questions, outline)
    db.execute("""
        UPDATE source_items SET status='triage_ready', error_message=NULL
        WHERE id=? AND status IN ('discovered','transcribed','error')
    """, (source_id,))
    db.commit()


def _normalize_questions(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    questions = []
    for item in value[:3]:
        if not isinstance(item, dict):
            continue
        question = re.sub(r"\s+", " ", str(item.get("question", "")).strip())[:80]
        answer = re.sub(r"\s+", " ", str(item.get("answer", "")).strip())[:360]
        status = str(item.get("answer_status", "")).strip()
        if status not in {"answered", "partial", "question_only"}:
            status = "answered" if answer else "question_only"
        status_note = re.sub(r"\s+", " ", str(item.get("status_note", "")).strip())[:160]
        evidence = []
        for row in item.get("evidence", []) if isinstance(item.get("evidence"), list) else []:
            if not isinstance(row, dict):
                continue
            time = str(row.get("time", "")).strip()[:20]
            excerpt = re.sub(r"\s+", " ", str(row.get("excerpt", "")).strip())[:220]
            if time or excerpt:
                evidence.append({"time": time, "excerpt": excerpt})
        if status == "answered" and (not answer or not evidence):
            status = "partial" if answer else "question_only"
            status_note = status_note or ("答案缺少可验证的原文依据" if answer else "原文只提出了问题")
        elif status == "partial" and not status_note:
            status_note = "原文只提供了部分方向，不能补全为完整答案"
        elif status == "question_only" and not status_note:
            status_note = "原文提出了这个问题，但没有提供可验证答案"
        if question:
            questions.append({
                "question": question,
                "answer": answer,
                "evidence": evidence[:3],
                "answer_status": status,
                "status_note": status_note,
            })
    return questions


def _normalize_outline(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    overview = re.sub(r"\s+", " ", str(value.get("overview", "")).strip())[:220]
    sections = []
    for item in value.get("sections", [])[:5] if isinstance(value.get("sections"), list) else []:
        if not isinstance(item, dict):
            continue
        title = re.sub(r"\s+", " ", str(item.get("title", "")).strip())[:48]
        summary = re.sub(r"\s+", " ", str(item.get("summary", "")).strip())[:180]
        points = [re.sub(r"\s+", " ", str(point).strip())[:120]
                  for point in item.get("points", []) if str(point).strip()][:4]
        evidence = []
        for row in item.get("evidence", []) if isinstance(item.get("evidence"), list) else []:
            if not isinstance(row, dict):
                continue
            time = str(row.get("time", "")).strip()[:20]
            excerpt = re.sub(r"\s+", " ", str(row.get("excerpt", "")).strip())[:220]
            if time or excerpt:
                evidence.append({"time": time, "excerpt": excerpt})
        if title and summary and evidence:
            sections.append({"title": title, "summary": summary, "points": points, "evidence": evidence[:3]})
    return {"overview": overview, "sections": sections} if overview and sections else {}


def _legacy_triage_fields(questions: list[dict]) -> tuple[str, list[str], list[dict]]:
    defaults = ["未提取到独立问题", "缺少可验证答案", "建议人工查看原文"]
    points = [question["question"] for question in questions]
    while len(points) < 3:
        points.append(defaults[len(points)])
    summary = f"①{points[0][:13]}；②{points[1][:13]}；③{points[2][:13]}"[:50]
    evidence = []
    for index, question in enumerate(questions, 1):
        evidence.extend({"question_index": index, "point": question["question"], **row}
                        for row in question["evidence"])
    return summary, points[:3], evidence


def _index_triage(db: sqlite3.Connection, source_id: int, questions: list[dict], outline: dict | None = None) -> None:
    db.execute("DELETE FROM triage_fts WHERE source_item_id=?", (source_id,))
    outline = outline or {}
    if outline.get("sections"):
        db.execute("""
            INSERT INTO triage_fts(source_item_id, question_text, answer_text) VALUES(?,?,?)
        """, (
            source_id,
            " ".join(section["title"] for section in outline["sections"]),
            " ".join([outline.get("overview", "")] + [
                " ".join([section["summary"], *section["points"]]) for section in outline["sections"]
            ]),
        ))
    elif questions:
        db.execute("""
            INSERT INTO triage_fts(source_item_id, question_text, answer_text) VALUES(?,?,?)
        """, (
            source_id,
            " ".join(question["question"] for question in questions),
            " ".join(question["answer"] for question in questions),
        ))


def update_triage_questions(db: sqlite3.Connection, source_id: int, questions: object) -> dict:
    normalized = _normalize_questions(questions)
    if not normalized:
        raise ValueError("请至少保留一个问题")
    if not db.execute("SELECT 1 FROM triage WHERE source_item_id=?", (source_id,)).fetchone():
        raise ValueError("这条收藏尚未生成审核内容")
    summary, points, evidence = _legacy_triage_fields(normalized)
    db.execute("""
        UPDATE triage SET summary_50=?, point_1=?, point_2=?, point_3=?, evidence_json=?,
            key_questions_json=?, usable_content_start=?, created_at=? WHERE source_item_id=?
    """, (
        summary, points[0], points[1], points[2], as_json(evidence), as_json(normalized),
        next((row["time"] for question in normalized for row in question["evidence"] if row["time"]), None),
        now_iso(), source_id,
    ))
    _index_triage(db, source_id, normalized)
    db.commit()
    return source_detail(db, source_id) or {}


def review_queue(db: sqlite3.Connection, now: str | None = None, limit: int = 100) -> dict[str, list[dict]]:
    current = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
    cutoff = (current - timedelta(hours=48)).isoformat()
    rows = list_sources(db, status="triage_ready", limit=limit)
    recent, historical = [], []
    for row in rows:
        saved = row.get("saved_at") or row.get("imported_at")
        (recent if saved and saved >= cutoff else historical).append(row)
    # Approved sources must remain reachable until their knowledge nodes have been created.
    return {
        "recent": recent,
        "historical": historical,
        "approved": list_sources(db, status="approved", limit=limit),
    }


def _unit_values(unit: dict) -> tuple:
    return (
        unit.get("title", "").strip(), unit.get("method_type", ""), unit.get("when_to_use", ""),
        as_json(unit.get("steps", [])), as_json(unit.get("constraints", [])),
        as_json(unit.get("common_questions", [])), as_json(unit.get("common_symptoms", [])),
        as_json(unit.get("keywords", [])), as_json(unit.get("topic_tags", [])),
        unit.get("status", "approved"),
    )


def _index_unit(db: sqlite3.Connection, unit_id: int) -> None:
    unit = _decoded(db.execute("SELECT * FROM knowledge_units WHERE id=?", (unit_id,)).fetchone())
    if not unit:
        return
    db.execute("DELETE FROM unit_fts WHERE knowledge_unit_id=?", (unit_id,))
    db.execute("""
        INSERT INTO unit_fts(knowledge_unit_id, title, when_to_use, steps, constraints,
            common_questions, common_symptoms, keywords, topic_tags)
        VALUES(?,?,?,?,?,?,?,?,?)
    """, (
        unit_id, unit["title"], unit["when_to_use"], " ".join(unit["steps"]),
        " ".join(unit["constraints"]), " ".join(unit["common_questions"]),
        " ".join(unit["common_symptoms"]), " ".join(unit["keywords"]),
        " ".join(unit["topic_tags"]),
    ))


def add_unit(db: sqlite3.Connection, unit: dict) -> int:
    if not unit.get("title", "").strip():
        raise ValueError("knowledge unit title is required")
    stamp = now_iso()
    db.execute("""
        INSERT INTO knowledge_units(title, method_type, when_to_use, steps, constraints,
            common_questions, common_symptoms, keywords, topic_tags, status, created_at, updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
    """, (*_unit_values(unit), stamp, stamp))
    unit_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
    _index_unit(db, unit_id)
    db.commit()
    return unit_id


def update_unit(db: sqlite3.Connection, unit_id: int, unit: dict) -> dict:
    current = _decoded(db.execute("SELECT * FROM knowledge_units WHERE id=?", (unit_id,)).fetchone())
    if not current:
        raise ValueError(f"knowledge unit not found: {unit_id}")
    merged = {**current, **unit}
    if not merged.get("title", "").strip():
        raise ValueError("knowledge unit title is required")
    db.execute("""
        UPDATE knowledge_units SET title=?, method_type=?, when_to_use=?, steps=?, constraints=?,
            common_questions=?, common_symptoms=?, keywords=?, topic_tags=?, status=?, updated_at=?
        WHERE id=?
    """, (*_unit_values(merged), now_iso(), unit_id))
    _index_unit(db, unit_id)
    db.commit()
    return get_unit(db, unit_id)


def link_unit_source(db: sqlite3.Connection, unit_id: int, source_id: int, **fields: object) -> None:
    source = db.execute("SELECT status FROM source_items WHERE id=?", (source_id,)).fetchone()
    if not source:
        raise ValueError(f"source not found: {source_id}")
    if source[0] not in {"approved", "curated"}:
        raise ValueError("knowledge units require explicit source approval")
    db.execute("""
        INSERT INTO unit_sources(knowledge_unit_id, source_item_id, segment_start, segment_end,
            contribution_type, new_points) VALUES(?,?,?,?,?,?)
        ON CONFLICT(knowledge_unit_id, source_item_id) DO UPDATE SET
            segment_start=excluded.segment_start, segment_end=excluded.segment_end,
            contribution_type=excluded.contribution_type, new_points=excluded.new_points
    """, (
        unit_id, source_id, fields.get("segment_start"), fields.get("segment_end"),
        fields.get("contribution_type", "primary"), as_json(fields.get("new_points", [])),
    ))
    db.commit()


def _seconds(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        parts = str(value).split(":")
        try:
            total = 0.0
            for part in parts:
                total = total * 60 + float(part)
            return max(0, int(total))
        except ValueError:
            return None


def _source_url_at(url: str, start: object = None) -> str:
    if "bilibili.com" not in url:
        return url
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    page = re.search(r"(?:^|[?&])p=(\d+)", parts.fragment)
    if page and "p" not in query:
        query["p"] = page.group(1)
    seconds = _seconds(start)
    if seconds is not None:
        query["t"] = str(seconds)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _source_link(row: dict) -> dict:
    return {**row, "source_url_at": _source_url_at(
        row.get("canonical_url", ""), row.get("segment_start")
    )}


def get_unit(db: sqlite3.Connection, unit_id: int) -> dict | None:
    unit = _decoded(db.execute("SELECT * FROM knowledge_units WHERE id=?", (unit_id,)).fetchone())
    if not unit:
        return None
    unit["sources"] = [_source_link(_decoded(row)) for row in db.execute("""
        SELECT s.id, s.title, s.canonical_url, s.platform, s.timestamped_transcript_path,
               us.segment_start, us.segment_end, us.contribution_type, us.new_points
        FROM unit_sources us JOIN source_items s ON s.id=us.source_item_id
        WHERE us.knowledge_unit_id=? ORDER BY s.imported_at DESC
    """, (unit_id,)).fetchall()]
    return unit


def list_units(db: sqlite3.Connection, limit: int = 100) -> list[dict]:
    ids = [row[0] for row in db.execute(
        "SELECT id FROM knowledge_units ORDER BY updated_at DESC LIMIT ?", (limit,)
    )]
    return [get_unit(db, unit_id) for unit_id in ids]


def add_topic(db: sqlite3.Connection, title: str, *, description: str = "",
              topic_type: str = "", parent_id: int | None = None, position: int = 0) -> int:
    title = title.strip()
    if not title:
        raise ValueError("topic title is required")
    if parent_id is not None and not db.execute(
        "SELECT 1 FROM knowledge_topics WHERE id=?", (parent_id,)
    ).fetchone():
        raise ValueError(f"parent topic not found: {parent_id}")
    existing = db.execute(
        "SELECT id FROM knowledge_topics WHERE title=? AND parent_id IS ?",
        (title, parent_id),
    ).fetchone()
    stamp = now_iso()
    if existing:
        topic_id = int(existing[0])
        db.execute("""
            UPDATE knowledge_topics SET description=CASE WHEN ? != '' THEN ? ELSE description END,
                topic_type=CASE WHEN ? != '' THEN ? ELSE topic_type END, position=?, updated_at=? WHERE id=?
        """, (description, description, topic_type, topic_type, position, stamp, topic_id))
    else:
        db.execute("""
            INSERT INTO knowledge_topics(title, description, topic_type, parent_id, position, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?)
        """, (title, description, topic_type, parent_id, position, stamp, stamp))
        topic_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
    db.commit()
    return topic_id


def link_topic_unit(db: sqlite3.Connection, topic_id: int, unit_id: int,
                    position: int = 0) -> None:
    if not db.execute("SELECT 1 FROM knowledge_topics WHERE id=?", (topic_id,)).fetchone():
        raise ValueError(f"topic not found: {topic_id}")
    if not db.execute("SELECT 1 FROM knowledge_units WHERE id=?", (unit_id,)).fetchone():
        raise ValueError(f"knowledge unit not found: {unit_id}")
    db.execute("""
        INSERT INTO topic_units(topic_id, knowledge_unit_id, position) VALUES(?,?,?)
        ON CONFLICT(topic_id, knowledge_unit_id) DO UPDATE SET position=excluded.position
    """, (topic_id, unit_id, position))
    db.commit()


def set_primary_topic(db: sqlite3.Connection, topic_id: int, unit_id: int,
                      position: int = 0) -> None:
    """Move a knowledge node to one deliberate primary place in the tree."""
    if not db.execute("SELECT 1 FROM knowledge_topics WHERE id=?", (topic_id,)).fetchone():
        raise ValueError(f"topic not found: {topic_id}")
    if not db.execute("SELECT 1 FROM knowledge_units WHERE id=?", (unit_id,)).fetchone():
        raise ValueError(f"knowledge unit not found: {unit_id}")
    db.execute("DELETE FROM topic_units WHERE knowledge_unit_id=?", (unit_id,))
    db.execute(
        "INSERT INTO topic_units(topic_id, knowledge_unit_id, position) VALUES(?,?,?)",
        (topic_id, unit_id, position),
    )
    db.commit()


def knowledge_tree(db: sqlite3.Connection) -> list[dict]:
    topics = {
        row["id"]: {**dict(row), "kind": "topic", "children": [], "units": []}
        for row in db.execute("""
            SELECT id, title, description, topic_type, parent_id, position
            FROM knowledge_topics ORDER BY position, id
        """)
    }
    linked_ids = set()
    for row in db.execute("""
        SELECT topic_id, knowledge_unit_id FROM topic_units
        ORDER BY topic_id, position, knowledge_unit_id
    """):
        topic = topics.get(row["topic_id"])
        unit = get_unit(db, row["knowledge_unit_id"])
        if topic and unit:
            topic["units"].append(unit)
            linked_ids.add(unit["id"])
    roots = []
    for topic in topics.values():
        parent = topics.get(topic["parent_id"])
        (parent["children"] if parent else roots).append(topic)
    ungrouped = [unit for unit in list_units(db, 500) if unit["id"] not in linked_ids]
    if ungrouped:
        roots.append({
            "id": 0, "title": "未分类知识", "description": "尚未加入主题树的知识点。",
            "parent_id": None, "position": 9999, "kind": "topic",
            "children": [], "units": ungrouped,
        })
    return roots


def source_units(db: sqlite3.Connection, source_id: int) -> list[dict]:
    """Return only the knowledge nodes whose provenance includes this source."""
    ids = [row[0] for row in db.execute("""
        SELECT knowledge_unit_id FROM unit_sources
        WHERE source_item_id=? ORDER BY knowledge_unit_id
    """, (source_id,))]
    return [unit for unit_id in ids if (unit := get_unit(db, unit_id))]


def _ensure_topic_path(db: sqlite3.Connection, root_id: int, path: list[str],
                       *, topic_type: str = "", descriptions: dict[tuple[str, ...], str] | None = None) -> int:
    parent_id = root_id
    walked: list[str] = []
    for position, title in enumerate(path):
        walked.append(title)
        parent_id = add_topic(
            db, title,
            description=(descriptions or {}).get(tuple(walked), ""),
            parent_id=parent_id, position=position,
        )
    return parent_id


def apply_organization_plan(db: sqlite3.Connection, source_id: int, plan: dict) -> dict:
    """Apply a user-confirmed plan; never infer or move nodes outside this source."""
    source = get_source(db, source_id)
    if not source or source["status"] not in {"approved", "curated"}:
        raise ValueError("只有已批准的内容可以写入知识库")
    allowed = {unit["id"] for unit in source_units(db, source_id)}
    if not allowed:
        raise ValueError("这条内容还没有可归档的知识点")
    root = plan.get("root") if isinstance(plan.get("root"), dict) else {}
    root_mode = str(root.get("mode", "new"))
    topic_type = str(plan.get("architecture_type", "")).strip()[:40]
    if root_mode == "existing":
        try:
            root_id = int(root.get("existing_topic_id"))
        except (TypeError, ValueError):
            raise ValueError("请选择一个已有主题")
        if not db.execute("SELECT 1 FROM knowledge_topics WHERE id=?", (root_id,)).fetchone():
            raise ValueError("选择的已有主题不存在")
    else:
        root_id = add_topic(
            db, str(root.get("title", "")),
            description=str(root.get("description", ""))[:240], topic_type=topic_type,
        )
    branch_paths: set[tuple[str, ...]] = {()}
    descriptions: dict[tuple[str, ...], str] = {}
    for branch in plan.get("branches", [])[:12] if isinstance(plan.get("branches"), list) else []:
        if not isinstance(branch, dict):
            continue
        path = [str(part).strip()[:80] for part in branch.get("path", [])
                if str(part).strip()][:3]
        if not path:
            continue
        branch_paths.add(tuple(path))
        descriptions[tuple(path)] = str(branch.get("description", ""))[:240]
    path_ids = {path: _ensure_topic_path(db, root_id, list(path), descriptions=descriptions)
                for path in sorted(branch_paths, key=lambda item: (len(item), item))}
    assigned = []
    for assignment in plan.get("assignments", [])[:len(allowed)] if isinstance(plan.get("assignments"), list) else []:
        if not isinstance(assignment, dict):
            continue
        try:
            unit_id = int(assignment.get("unit_id"))
        except (TypeError, ValueError):
            continue
        if unit_id not in allowed:
            continue
        path = tuple(str(part).strip()[:80] for part in assignment.get("path", [])
                     if str(part).strip())[:3]
        target_id = path_ids.get(path, root_id)
        set_primary_topic(db, target_id, unit_id)
        assigned.append(unit_id)
    # A proposal should not strand a source node merely because its title did
    # not match one of the suggested branches.
    for unit_id in allowed - set(assigned):
        set_primary_topic(db, root_id, unit_id)
        assigned.append(unit_id)
    return {"root_id": root_id, "unit_ids": assigned, "topic_type": topic_type}


def index_source(db: sqlite3.Connection, source_id: int, text: str) -> None:
    db.execute("DELETE FROM source_fts WHERE source_item_id=?", (source_id,))
    db.execute("""
        INSERT INTO source_fts(source_item_id, title, transcript_text)
        SELECT id, title, ? FROM source_items WHERE id=?
    """, (text, source_id))
    db.commit()


def _fts_query(query: str) -> str:
    tokens = [token for token in re.findall(r"[A-Za-z0-9_.+#-]+|[\u4e00-\u9fff]{2,8}", query) if token]
    return " OR ".join(f'"{token.replace(chr(34), "")}"' for token in tokens[:12])


def basic_query_terms(query: str) -> list[str]:
    """Remove common question framing so literal search still works without an LLM."""
    stripped = re.sub(
        r"怎样|怎么|如何|为什么|什么是|什么|是否|可以|应该|请问|帮我|告诉我|"
        r"有哪些|有没有|一种|一个|这个|那个|进行|使用|方法|做|吗|呢|的",
        " ",
        query,
    )
    segments = re.findall(r"[A-Za-z][A-Za-z0-9_.+#-]{1,}|[\u4e00-\u9fff]{2,12}", stripped)
    terms = [query.strip(), *segments]
    for segment in segments:
        if re.fullmatch(r"[\u4e00-\u9fff]{4,}", segment):
            terms.extend(segment[index:index + 2] for index in range(len(segment) - 1))
    return list(dict.fromkeys(term for term in terms if len(term) >= 2))[:32]


def search(db: sqlite3.Connection, query: str, limit: int = 20, *, include_unreviewed: bool = False) -> list[dict]:
    query = query.strip()
    if not query:
        return []
    results, seen = [], set()
    source_statuses = "('approved','curated')" if not include_unreviewed else \
        "('approved','curated','triage_ready','transcribed','legacy_imported')"
    terms = basic_query_terms(query)
    match = _fts_query(" ".join(terms[1:] or terms))
    if match:
        for kind, sql in (
            ("knowledge_unit", """
                SELECT u.id, bm25(unit_fts) AS score FROM unit_fts
                JOIN knowledge_units u ON u.id=unit_fts.knowledge_unit_id
                WHERE unit_fts MATCH ? AND u.status IN ('approved','curated') ORDER BY score LIMIT ?
            """),
            ("source", """
                SELECT s.id, bm25(triage_fts) AS score FROM triage_fts
                JOIN source_items s ON s.id=triage_fts.source_item_id
                WHERE triage_fts MATCH ? AND s.status IN %s
                ORDER BY score LIMIT ?
            """ % source_statuses),
            ("source", """
                SELECT s.id, bm25(source_fts) AS score FROM source_fts
                JOIN source_items s ON s.id=source_fts.source_item_id
                WHERE source_fts MATCH ? AND s.status IN %s
                ORDER BY score LIMIT ?
            """ % source_statuses),
        ):
            try:
                rows = db.execute(sql, (match, limit)).fetchall()
            except sqlite3.OperationalError:
                rows = []
            for row in rows:
                key = (kind, row["id"])
                if key in seen:
                    continue
                seen.add(key)
                value = get_unit(db, row["id"]) if kind == "knowledge_unit" else source_detail(db, row["id"])
                results.append({"kind": kind, "score": row["score"], **value})
    fallback = []
    for term in basic_query_terms(query):
        like = f"%{term}%"
        fallback.extend(
            ("knowledge_unit", row[0]) for row in db.execute("""
                SELECT id FROM knowledge_units WHERE status IN ('approved','curated') AND
                (title LIKE ? OR when_to_use LIKE ? OR steps LIKE ? OR constraints LIKE ? OR
                 common_questions LIKE ? OR common_symptoms LIKE ? OR keywords LIKE ? OR topic_tags LIKE ?) LIMIT ?
            """, (like, like, like, like, like, like, like, like, limit))
        )
        fallback.extend(
            ("source", row[0]) for row in db.execute("""
                SELECT s.id FROM source_items s
                LEFT JOIN source_fts f ON f.source_item_id=s.id
                LEFT JOIN triage t ON t.source_item_id=s.id
                WHERE s.status IN %s
                  AND (s.title LIKE ? OR f.transcript_text LIKE ? OR t.key_questions_json LIKE ? OR t.video_outline_json LIKE ?) LIMIT ?
            """ % source_statuses, (like, like, like, like, limit))
        )
    for kind, item_id in fallback:
        if (kind, item_id) in seen:
            continue
        seen.add((kind, item_id))
        value = get_unit(db, item_id) if kind == "knowledge_unit" else source_detail(db, item_id)
        results.append({"kind": kind, "score": None, **value})
    ranking_terms = [term.lower() for term in (terms[1:] or terms)]

    def literal_rank(item: dict) -> tuple[int, int, float]:
        title = str(item.get("title", "")).lower()
        body = " ".join(str(item.get(key, "")) for key in ("when_to_use", "summary_50"))
        outline = item.get("video_outline") or {}
        if isinstance(outline, dict):
            body += " " + str(outline.get("overview", ""))
            for section in outline.get("sections", []) if isinstance(outline.get("sections"), list) else []:
                if isinstance(section, dict):
                    body += " " + " ".join([
                        str(section.get("title", "")), str(section.get("summary", "")),
                        " ".join(str(point) for point in section.get("points", [])),
                    ])
        for key in ("steps", "constraints", "common_questions", "common_symptoms", "keywords", "topic_tags"):
            values = item.get(key) or []
            body += " " + (" ".join(values) if isinstance(values, list) else str(values))
        question_text = " ".join(
            f"{row.get('question', '')} {row.get('answer', '')}"
            for row in item.get("key_questions", []) if isinstance(row, dict)
        ).lower()
        title_hits = sum(term in title for term in ranking_terms)
        body_hits = sum(term in body.lower() for term in ranking_terms)
        question_hits = sum(term in question_text for term in ranking_terms)
        kind_boost = 1 if item.get("kind") == "knowledge_unit" else 0
        fts_score = -float(item["score"]) if item.get("score") is not None else 0.0
        return title_hits * 4 + question_hits * 3 + body_hits + kind_boost, title_hits, fts_score

    results.sort(key=literal_rank, reverse=True)
    return results[:limit]


def log_search(db: sqlite3.Connection, query: str, returned_ids: Iterable[int]) -> int:
    db.execute("""
        INSERT INTO search_events(query, query_time, returned_unit_ids) VALUES(?,?,?)
    """, (query, now_iso(), as_json(list(returned_ids))))
    event_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
    db.commit()
    return event_id


def record_search_feedback(db: sqlite3.Connection, event_id: int, feedback: str,
                           clicked_unit_id: int | None = None, source_item_id: int | None = None) -> None:
    db.execute("""
        UPDATE search_events SET user_feedback=?, clicked_unit_id=?, source_item_id=? WHERE id=?
    """, (feedback, clicked_unit_id, source_item_id, event_id))
    db.commit()


def create_job(db: sqlite3.Connection, source_id: int, job_type: str, payload: dict | None = None) -> int:
    existing = db.execute("""
        SELECT id FROM processing_jobs
        WHERE source_item_id=? AND job_type=? AND status IN ('queued','running')
        ORDER BY id DESC LIMIT 1
    """, (source_id, job_type)).fetchone()
    if existing:
        return int(existing[0])
    db.execute("""
        INSERT INTO processing_jobs(source_item_id, job_type, status, phase, progress, payload, created_at)
        VALUES(?,?,'queued','queued',0,?,?)
    """, (source_id, job_type, json.dumps(payload or {}, ensure_ascii=False), now_iso()))
    job_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
    db.commit()
    return job_id


def get_job(db: sqlite3.Connection, job_id: int) -> dict | None:
    return _decoded(db.execute("""
        SELECT j.*, s.title, s.canonical_url FROM processing_jobs j
        JOIN source_items s ON s.id=j.source_item_id WHERE j.id=?
    """, (job_id,)).fetchone())


def list_jobs(db: sqlite3.Connection, limit: int = 50) -> list[dict]:
    return [_decoded(row) for row in db.execute("""
        SELECT j.*, s.title, s.canonical_url FROM processing_jobs j
        JOIN source_items s ON s.id=j.source_item_id
        ORDER BY CASE j.status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,
                 j.created_at DESC LIMIT ?
    """, (limit,)).fetchall()]


def next_job(db: sqlite3.Connection) -> dict | None:
    row = db.execute("""
        SELECT id FROM processing_jobs
        WHERE status='queued' AND (retry_after IS NULL OR retry_after <= ?)
        ORDER BY COALESCE(retry_after, created_at), id LIMIT 1
    """, (now_iso(),)).fetchone()
    if not row:
        return None
    job_id = int(row[0])
    db.execute("""
        UPDATE processing_jobs SET status='running', phase='starting', progress=1,
            started_at=?, finished_at=NULL, retry_after=NULL, error_message=NULL WHERE id=?
    """, (now_iso(), job_id))
    db.commit()
    return get_job(db, job_id)


def update_job(db: sqlite3.Connection, job_id: int, *, status: str | None = None,
               phase: str | None = None, progress: int | None = None,
               message: str | None = None, error: str | None = None) -> None:
    if status and status not in JOB_STATUSES:
        raise ValueError(f"invalid job status: {status}")
    fields, values = [], []
    for column, value in (("status", status), ("phase", phase), ("progress", progress),
                          ("message", message), ("error_message", error)):
        if value is not None:
            fields.append(f"{column}=?")
            values.append(value)
    if status in {"succeeded", "failed"}:
        fields.append("finished_at=?")
        values.append(now_iso())
    if not fields:
        return
    values.append(job_id)
    db.execute(f"UPDATE processing_jobs SET {', '.join(fields)} WHERE id=?", values)
    db.commit()


def retry_job(db: sqlite3.Connection, job_id: int) -> None:
    if not db.execute("SELECT 1 FROM processing_jobs WHERE id=?", (job_id,)).fetchone():
        raise ValueError(f"job not found: {job_id}")
    db.execute("""
        UPDATE processing_jobs SET status='queued', phase='queued', progress=0, message='',
            started_at=NULL, finished_at=NULL, error_message=NULL, attempt_count=0,
            retry_after=NULL WHERE id=?
    """, (job_id,))
    db.commit()


def schedule_model_retry(db: sqlite3.Connection, job_id: int, *, delay_seconds: int,
                         attempt_count: int, error: str) -> None:
    retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
    local_time = retry_at.astimezone().strftime("%H:%M")
    db.execute("""
        UPDATE processing_jobs
        SET status='queued', phase='waiting_for_model', progress=0,
            message=?, error_message=?, attempt_count=?, retry_after=?,
            started_at=NULL, finished_at=NULL
        WHERE id=?
    """, (
        f"模型服务暂时繁忙，将在 {local_time} 自动重试（第 {attempt_count} 次）",
        error, attempt_count, retry_at.replace(microsecond=0).isoformat(), job_id,
    ))
    db.commit()


def requeue_interrupted_jobs(db: sqlite3.Connection) -> int:
    cursor = db.execute("""
        UPDATE processing_jobs SET status='queued', phase='resume', progress=0,
            message='程序重启，准备从已有进度恢复', started_at=NULL
        WHERE status='running'
    """)
    db.commit()
    return cursor.rowcount
