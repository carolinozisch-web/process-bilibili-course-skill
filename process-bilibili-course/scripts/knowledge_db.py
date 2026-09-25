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


SCHEMA_VERSION = 2
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
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON processing_jobs(status, created_at);
CREATE VIRTUAL TABLE IF NOT EXISTS source_fts USING fts5(
    source_item_id UNINDEXED, title, transcript_text
);
CREATE VIRTUAL TABLE IF NOT EXISTS unit_fts USING fts5(
    knowledge_unit_id UNINDEXED, title, when_to_use, steps, constraints,
    common_questions, common_symptoms, keywords, topic_tags
);
"""

JSON_FIELDS = {
    "keywords", "topic_candidates", "source_signals", "possible_duplicates",
    "new_points", "evidence_json", "steps", "constraints", "common_questions",
    "common_symptoms", "topic_tags", "payload", "returned_unit_ids",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def as_json(value: object) -> str:
    return json.dumps(value if value is not None else [], ensure_ascii=False)


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _backup_legacy_database(path: Path) -> Path | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    probe = sqlite3.connect(str(path))
    try:
        legacy = _table_exists(probe, "source_items") and not _table_exists(probe, "schema_meta")
    finally:
        probe.close()
    if not legacy:
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
    _ensure_column(db, "triage", "ai_mode TEXT NOT NULL DEFAULT 'basic'")
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
    _backup_legacy_database(path)
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
               t.new_points, t.usable_content_start, t.evidence_json, t.ai_mode
        FROM source_items s LEFT JOIN triage t ON t.source_item_id=s.id
        WHERE s.id=?
    """, (source_id,)).fetchone())
    if not source:
        return None
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
    if not all(marker in summary for marker in ("①", "②", "③")):
        raise ValueError("summary_50 must contain ①, ② and ③")
    db.execute("""
        INSERT INTO triage(source_item_id, summary_50, point_1, point_2, point_3, keywords,
            topic_candidates, source_signals, possible_duplicates, new_points, usable_content_start,
            evidence_json, ai_mode, created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(source_item_id) DO UPDATE SET
            summary_50=excluded.summary_50, point_1=excluded.point_1, point_2=excluded.point_2,
            point_3=excluded.point_3, keywords=excluded.keywords,
            topic_candidates=excluded.topic_candidates, source_signals=excluded.source_signals,
            possible_duplicates=excluded.possible_duplicates, new_points=excluded.new_points,
            usable_content_start=excluded.usable_content_start, evidence_json=excluded.evidence_json,
            ai_mode=excluded.ai_mode, created_at=excluded.created_at
    """, (
        source_id, summary, data.get("point_1", ""), data.get("point_2", ""), data.get("point_3", ""),
        as_json(data.get("keywords", [])), as_json(data.get("topic_candidates", [])),
        as_json(data.get("source_signals", [])), as_json(data.get("possible_duplicates", [])),
        as_json(data.get("new_points", [])), data.get("usable_content_start"),
        as_json(data.get("evidence", [])), data.get("ai_mode", "basic"), data.get("created_at") or now_iso(),
    ))
    db.execute("""
        UPDATE source_items SET status='triage_ready', error_message=NULL
        WHERE id=? AND status IN ('discovered','transcribed','error')
    """, (source_id,))
    db.commit()


def review_queue(db: sqlite3.Connection, now: str | None = None, limit: int = 100) -> dict[str, list[dict]]:
    current = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
    cutoff = (current - timedelta(hours=48)).isoformat()
    rows = list_sources(db, status="triage_ready", limit=limit)
    recent, historical = [], []
    for row in rows:
        saved = row.get("saved_at") or row.get("imported_at")
        (recent if saved and saved >= cutoff else historical).append(row)
    return {"recent": recent, "historical": historical}


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


def _source_link(row: dict) -> dict:
    start = row.get("segment_start")
    url = row.get("canonical_url", "")
    if start is not None and "bilibili.com" in url:
        joiner = "&" if "?" in url else "?"
        url = f"{url}{joiner}t={int(start)}"
    return {**row, "source_url_at": url}


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
        r"有哪些|有没有|一种|一个|这个|那个|进行|使用|判断|解释|方法|变量|做|可靠|吗|呢|的",
        " ",
        query,
    )
    segments = re.findall(r"[A-Za-z][A-Za-z0-9_.+#-]{1,}|[\u4e00-\u9fff]{2,12}", stripped)
    terms = [query.strip(), *segments]
    for segment in segments:
        if re.fullmatch(r"[\u4e00-\u9fff]{4,}", segment):
            terms.extend(segment[index:index + 2] for index in range(len(segment) - 1))
    return list(dict.fromkeys(term for term in terms if len(term) >= 2))[:32]


def search(db: sqlite3.Connection, query: str, limit: int = 20) -> list[dict]:
    query = query.strip()
    if not query:
        return []
    results, seen = [], set()
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
                SELECT s.id, bm25(source_fts) AS score FROM source_fts
                JOIN source_items s ON s.id=source_fts.source_item_id
                WHERE source_fts MATCH ? AND s.status IN
                    ('approved','curated','triage_ready','transcribed','legacy_imported')
                ORDER BY score LIMIT ?
            """),
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
                (title LIKE ? OR when_to_use LIKE ? OR steps LIKE ? OR keywords LIKE ?) LIMIT ?
            """, (like, like, like, like, limit))
        )
        fallback.extend(
            ("source", row[0]) for row in db.execute("""
                SELECT s.id FROM source_items s LEFT JOIN source_fts f ON f.source_item_id=s.id
                WHERE s.status IN ('approved','curated','triage_ready','transcribed','legacy_imported')
                  AND (s.title LIKE ? OR f.transcript_text LIKE ?) LIMIT ?
            """, (like, like, limit))
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
        body += " " + " ".join(item.get("steps", []))
        title_hits = sum(term in title for term in ranking_terms)
        body_hits = sum(term in body.lower() for term in ranking_terms)
        kind_boost = 1 if item.get("kind") == "knowledge_unit" else 0
        fts_score = -float(item["score"]) if item.get("score") is not None else 0.0
        return title_hits * 4 + body_hits + kind_boost, title_hits, fts_score

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
    row = db.execute("SELECT id FROM processing_jobs WHERE status='queued' ORDER BY created_at, id LIMIT 1").fetchone()
    if not row:
        return None
    job_id = int(row[0])
    db.execute("""
        UPDATE processing_jobs SET status='running', phase='starting', progress=1,
            started_at=?, finished_at=NULL, error_message=NULL WHERE id=?
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
            started_at=NULL, finished_at=NULL, error_message=NULL WHERE id=?
    """, (job_id,))
    db.commit()


def requeue_interrupted_jobs(db: sqlite3.Connection) -> int:
    cursor = db.execute("""
        UPDATE processing_jobs SET status='queued', phase='resume', progress=0,
            message='程序重启，准备从已有进度恢复', started_at=NULL
        WHERE status='running'
    """)
    db.commit()
    return cursor.rowcount
