#!/usr/bin/env python3
"""Dry-run or back up and repair uniquely grounded out-of-range outline times."""

from __future__ import annotations

import argparse
import json
import sqlite3
from copy import deepcopy
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from transcript_evidence import EvidenceError, resolve_evidence, timestamp_seconds, transcript_segments, validate_time


def invalid_time(value: object, duration: object) -> bool:
    try:
        validate_time(value, duration)
        return False
    except EvidenceError:
        return True


def plan_repairs(db: sqlite3.Connection, workspace: Path) -> tuple[list[dict], list[dict]]:
    plans, unresolved = [], []
    rows = db.execute("""
        SELECT s.id, s.duration_seconds, s.timestamped_transcript_path,
               t.video_outline_json, t.evidence_json, t.usable_content_start
        FROM source_items s JOIN triage t ON t.source_item_id=s.id
        WHERE t.video_outline_json != '{}'
    """).fetchall()
    for row in rows:
        source = dict(row)
        original = json.loads(source['video_outline_json'])
        duration = source['duration_seconds']
        if not any(invalid_time(ev.get('time'), duration)
                   for section in original.get('sections', []) for ev in section.get('evidence', [])):
            continue
        path = Path(source['timestamped_transcript_path'] or '')
        if not path.is_absolute():
            path = workspace / path
        try:
            segments = transcript_segments(path.read_text(encoding='utf-8-sig'), duration)
            outline, corrections = deepcopy(original), []
            for section in outline.get('sections', []):
                for index, evidence in enumerate(section.get('evidence', [])):
                    if not invalid_time(evidence.get('time'), duration):
                        continue
                    grounded = resolve_evidence(evidence, segments)
                    if not grounded:
                        raise EvidenceError('原文证据没有唯一匹配，未自动修改')
                    corrections.append({'before': evidence['time'], 'after': grounded['time']})
                    section['evidence'][index] = grounded
            unit_updates = []
            for unit in db.execute("""
                SELECT u.id, u.title, us.segment_start, us.segment_end
                FROM unit_sources us JOIN knowledge_units u ON u.id=us.knowledge_unit_id
                WHERE us.source_item_id=?
            """, (source['id'],)):
                if invalid_time(unit['segment_end'], duration):
                    raise EvidenceError('节点结束时间无法可靠恢复，未自动修改')
                if not invalid_time(unit['segment_start'], duration):
                    continue
                indices = [i for i, section in enumerate(original['sections']) if section['title'] == unit['title']]
                if len(indices) != 1:
                    raise EvidenceError('节点与章节无法唯一对应，未自动修改')
                i = indices[0]
                old = timestamp_seconds(original['sections'][i]['evidence'][0].get('time'))
                if old != unit['segment_start']:
                    raise EvidenceError('节点起点与旧证据不一致，未自动修改')
                new = validate_time(outline['sections'][i]['evidence'][0]['time'], duration)
                unit_updates.append({'id': unit['id'], 'before': unit['segment_start'], 'after': new})
            plans.append({'source': source, 'outline': outline,
                          'corrections': corrections, 'units': unit_updates})
        except (OSError, EvidenceError, ValueError, KeyError, IndexError) as exc:
            unresolved.append({'source_id': source['id'], 'reason': str(exc)})
    return plans, unresolved


def apply_repairs(db: sqlite3.Connection, plans: list[dict], backup_dir: Path) -> Path:
    if db.execute("SELECT COUNT(*) FROM processing_jobs WHERE status='running'").fetchone()[0]:
        raise EvidenceError('有任务正在运行，请任务结束后再修复')
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = backup_dir / f'knowledge-before-evidence-repair-{stamp}.db'
    with closing(sqlite3.connect(backup)) as target:
        db.backup(target)
    db.execute('BEGIN IMMEDIATE')
    try:
        for plan in plans:
            source = plan['source']
            current = db.execute("""
                SELECT video_outline_json, evidence_json, usable_content_start
                FROM triage WHERE source_item_id=?
            """, (source['id'],)).fetchone()
            expected = tuple(source[key] for key in ('video_outline_json', 'evidence_json', 'usable_content_start'))
            if not current or tuple(current) != expected:
                raise EvidenceError('审核内容已发生变化，请重新检查后再修复')
            outline = plan['outline']
            evidence = [{"section_index": i, "point": section['title'], **ev}
                        for i, section in enumerate(outline['sections'], 1) for ev in section['evidence']]
            start = source['usable_content_start']
            if invalid_time(start, source['duration_seconds']):
                start = evidence[0]['time'] if evidence else None
            db.execute("""
                UPDATE triage SET video_outline_json=?, evidence_json=?, usable_content_start=?
                WHERE source_item_id=?
            """, (json.dumps(outline, ensure_ascii=False), json.dumps(evidence, ensure_ascii=False), start, source['id']))
            for unit in plan['units']:
                cursor = db.execute("""
                    UPDATE unit_sources SET segment_start=?
                    WHERE source_item_id=? AND knowledge_unit_id=? AND segment_start=?
                """, (unit['after'], source['id'], unit['id'], unit['before']))
                if cursor.rowcount != 1:
                    raise EvidenceError('节点已经变化，修复已取消')
        db.commit()
    except Exception:
        db.rollback()
        raise
    return backup


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--apply', action='store_true', help='Back up and apply; default is read-only')
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    path = workspace / '知识库' / 'knowledge.db'
    mode = 'rw' if args.apply else 'ro'
    db = sqlite3.connect(path.as_uri() + f'?mode={mode}', uri=True)
    db.row_factory = sqlite3.Row
    try:
        plans, unresolved = plan_repairs(db, workspace)
        backup = apply_repairs(db, plans, workspace / '知识库' / 'backups') if args.apply and plans else None
        print(json.dumps({'applied': bool(backup), 'backup': str(backup) if backup else None,
                          'sources': [{'source_id': p['source']['id'], 'times': p['corrections'],
                                       'nodes': p['units']} for p in plans],
                          'unresolved': unresolved}, ensure_ascii=False, indent=2))
    finally:
        db.close()


if __name__ == '__main__':
    main()
