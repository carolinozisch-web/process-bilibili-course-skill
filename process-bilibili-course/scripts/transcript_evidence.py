"""Bind evidence to transcript rows instead of trusting model-written times."""

from __future__ import annotations

import math
import re
from copy import deepcopy


class EvidenceError(ValueError):
    pass


def timestamp_seconds(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        return seconds if math.isfinite(seconds) and seconds >= 0 else None
    raw = str(value).strip()
    if not re.fullmatch(r"\d+(?::\d{2}){0,2}(?:\.\d+)?", raw):
        return None
    parts = [float(part) for part in raw.split(":")]
    if any(part >= 60 for part in parts[1:]):
        return None
    total = 0.0
    for part in parts:
        total = total * 60 + part
    return total if math.isfinite(total) else None


def validate_time(value: object, duration: object = None) -> float | None:
    if value in (None, ""):
        return None
    seconds = timestamp_seconds(value)
    bound = timestamp_seconds(duration)
    if seconds is None or (bound and seconds > bound):
        raise EvidenceError("证据时间无效或超过视频时长，请核对原文后重新提取")
    return seconds


def transcript_segments(transcript: str, duration: object = None) -> list[dict]:
    rows = []
    for line in transcript.splitlines():
        match = re.match(r"^\s*\[([^\]]+)\]\s*(.+)$", line)
        if not match:
            continue
        time, content = match.groups()
        seconds = validate_time(time, duration)
        if seconds is None:
            continue
        if rows and seconds < rows[-1]["start_seconds"]:
            raise EvidenceError("逐字稿时间顺序异常，请先核对原始转写")
        rows.append({"segment_id": f"S{len(rows) + 1:06d}", "time": time,
                     "start_seconds": seconds, "excerpt": content.strip()})
    return rows


def _plain(value: object) -> str:
    return re.sub(r"[\W_]+", "", str(value)).lower()


def resolve_evidence(evidence: dict, segments: list[dict]) -> dict | None:
    segment_id = evidence.get("segment_id")
    if segment_id:
        row = next((row for row in segments if row["segment_id"] == segment_id), None)
        return {**row, "excerpt": row["excerpt"][:220]} if row else None
    # Compatibility and repair: use only a unique verbatim match, never a
    # guessed hours/minutes conversion or a nearest-timestamp match.
    quote = _plain(evidence.get("excerpt", ""))
    if len(quote) < 4:
        return None
    matches = []
    for index, row in enumerate(segments):
        window = segments[index:index + 3]
        joined = ""
        for end, part in enumerate(window):
            joined += _plain(part["excerpt"])
            position = joined.find(quote)
            if position >= 0:
                if position < len(_plain(row["excerpt"])):
                    excerpt = " ".join(item["excerpt"] for item in window[:end + 1])[:220]
                    matches.append({**row, "excerpt": excerpt})
                break
    return matches[0] if len(matches) == 1 else None


def bind_outline(value: object, segments: list[dict]) -> dict:
    outline = deepcopy(value) if isinstance(value, dict) else {}
    sections = []
    for section in outline.get("sections", []) if isinstance(outline.get("sections"), list) else []:
        if not isinstance(section, dict):
            continue
        bound, seen = [], set()
        for raw in section.get("evidence", []) if isinstance(section.get("evidence"), list) else []:
            row = resolve_evidence(raw, segments) if isinstance(raw, dict) else None
            if row and row["segment_id"] not in seen:
                bound.append(row)
                seen.add(row["segment_id"])
        if not bound:
            raise EvidenceError("章节证据无法定位到逐字稿，未保存结果，请重新提取或人工核对")
        sections.append({**section, "evidence": bound[:3]})
    outline["sections"] = sections
    return outline


def validate_outline_times(outline: dict, duration: object = None) -> None:
    for section in outline.get("sections", []):
        for evidence in section.get("evidence", []):
            if validate_time(evidence.get("time"), duration) is None:
                raise EvidenceError("知识节点缺少可验证的证据时间")


def evidence_fields(row: dict) -> dict:
    return {key: row[key] for key in ("segment_id", "start_seconds") if key in row}
