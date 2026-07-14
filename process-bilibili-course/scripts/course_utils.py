#!/usr/bin/env python3
"""Maintain indexes, manifest note states, and validate a processed course."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path


LINK_RE = re.compile(r"\]\(<([^>]+)>\)")
HEADING_RE = re.compile(r"^#\s+\[(.+?)\]\(", re.MULTILINE)


def default_workspace() -> str:
    """Use an explicit environment override or the current working directory."""
    return os.environ.get("VIDEO_SUMMARY_HOME") or str(Path.cwd())


def roots(workspace: Path, course: str):
    course_root = workspace / "课程资料" / course
    work_root = workspace / "后台处理文件" / course
    return course_root, work_root, work_root / "course-manifest.json"


def load_manifest(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"ERROR: manifest not found: {path}")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_manifest(path: Path, manifest: dict) -> None:
    manifest["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def command_mark(args) -> None:
    _, _, manifest_path = roots(Path(args.workspace).resolve(), args.course)
    manifest = load_manifest(manifest_path)
    for item in manifest["episodes"]:
        if int(item["page"]) == args.episode:
            item["state"] = args.state
            item["detail"] = args.detail or item.get("detail", "")
            save_manifest(manifest_path, manifest)
            print(f"EP{args.episode:02d}={args.state}")
            return
    raise SystemExit(f"ERROR: episode {args.episode} not in manifest")


def command_index(args) -> None:
    course_root, _, manifest_path = roots(Path(args.workspace).resolve(), args.course)
    manifest = load_manifest(manifest_path)
    note_root = course_root / "01-详细笔记"
    entry_root = course_root / "00-课程入口"
    entry_root.mkdir(parents=True, exist_ok=True)
    lines = [f"# {args.course}：详细笔记目录", "", "点击标题打开详细笔记；在详细笔记顶部点击标题可打开逐字稿。", ""]
    active = [item for item in manifest["episodes"] if item.get("state") != "skipped_review"]
    for offset in range(0, len(active), 10):
        group = active[offset:offset + 10]
        if not group:
            continue
        lines.extend([f"## 第 {group[0]['page']}–{group[-1]['page']} 集", ""])
        for item in group:
            number = int(item["page"])
            episode_id = f"{number:02d}"
            note = note_root / f"{episode_id}-notes.md"
            title = f"第 {number} 集：{item['title']}"
            if note.exists():
                match = HEADING_RE.search(note.read_text(encoding="utf-8-sig"))
                if match:
                    title = match.group(1).replace("（详细整理）", "")
            lines.append(f"{number}. [{title}](<../01-详细笔记/{episode_id}-notes.md>)")
        lines.append("")
    target = entry_root / f"{args.course}-详细笔记目录.md"
    target.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(target)


def markdown_broken_links(root: Path) -> list[str]:
    broken = []
    for path in root.rglob("*.md"):
        text = path.read_text(encoding="utf-8-sig")
        for match in LINK_RE.finditer(text):
            target = match.group(1)
            if target.startswith("#") or "://" in target:
                continue
            resolved = path.parent / Path(target.replace("/", "\\" if sys.platform == "win32" else "/"))
            if not resolved.exists():
                broken.append(f"{path}: {target}")
    return broken


def command_validate(args) -> None:
    course_root, work_root, manifest_path = roots(Path(args.workspace).resolve(), args.course)
    manifest = load_manifest(manifest_path)
    problems = []
    counts = {"expected": 0, "notes": 0, "transcripts": 0, "audio": 0, "exceptions": 0, "skipped": 0}
    for item in manifest["episodes"]:
        number = int(item["page"])
        episode_id = f"{number:02d}"
        state = item.get("state", "pending")
        if state == "skipped_review":
            counts["skipped"] += 1
            continue
        counts["expected"] += 1
        note = course_root / "01-详细笔记" / f"{episode_id}-notes.md"
        transcript = course_root / "02-逐字稿" / f"{episode_id}-transcript.txt"
        audio = course_root / "03-音频" / f"{episode_id}-audio.mp3"
        if note.exists(): counts["notes"] += 1
        else: problems.append(f"EP{episode_id} missing note")
        if transcript.exists() and transcript.stat().st_size > 0: counts["transcripts"] += 1
        else: problems.append(f"EP{episode_id} missing transcript")
        if audio.exists() and audio.stat().st_size > 10_000: counts["audio"] += 1
        else: problems.append(f"EP{episode_id} missing/invalid audio")
        if state == "abnormal_short": counts["exceptions"] += 1
        if state not in {"notes_done", "validated", "abnormal_short"}:
            problems.append(f"EP{episode_id} nonterminal state={state}")
    cache_root = work_root / "原始缓存"
    temporary = (
        [path for path in cache_root.rglob("*") if path.is_file() and path.suffix.lower() in {".m4s", ".mp4", ".webm"}]
        if cache_root.exists() else []
    )
    if temporary:
        problems.append(f"temporary media files remain: {len(temporary)}")
    broken = markdown_broken_links(course_root)
    problems.extend(f"broken link: {item}" for item in broken)
    result = {"course": args.course, "counts": counts, "broken_links": len(broken), "problems": problems}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if problems:
        raise SystemExit(1)


def command_status(args) -> None:
    _, _, manifest_path = roots(Path(args.workspace).resolve(), args.course)
    manifest = load_manifest(manifest_path)
    states = {}
    for item in manifest["episodes"]:
        states[item.get("state", "pending")] = states.get(item.get("state", "pending"), 0) + 1
    print(json.dumps({"course": args.course, "updated": manifest.get("updated"), "states": states}, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("mark", "index", "validate", "status"))
    parser.add_argument("--workspace", default=default_workspace())
    parser.add_argument("--course", required=True)
    parser.add_argument("--episode", type=int)
    parser.add_argument("--state")
    parser.add_argument("--detail", default="")
    args = parser.parse_args()
    if args.command == "mark":
        if args.episode is None or not args.state:
            parser.error("mark requires --episode and --state")
        command_mark(args)
    elif args.command == "index": command_index(args)
    elif args.command == "validate": command_validate(args)
    else: command_status(args)


if __name__ == "__main__":
    main()
