#!/usr/bin/env python3
"""Inspect, download, convert, and locally transcribe a public Bilibili course."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse


BVID_RE = re.compile(r"BV[0-9A-Za-z]{10}", re.IGNORECASE)
COMPILATION_RE = re.compile(r"全集|合集|完整版|一口气|全\d+[集讲节]|完整课程")
INVALID_WINDOWS = re.compile(r'[<>:"/\\|?*]+')


def stamp(seconds: float) -> str:
    total = round(seconds * 1000)
    hours, rem = divmod(total, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def safe_name(value: str) -> str:
    value = INVALID_WINDOWS.sub("-", value).strip(" .-")
    value = re.sub(r"\s+", " ", value)
    return value[:80] or "B站课程"


def default_workspace() -> str:
    """Use an explicit environment override or the current working directory."""
    return os.environ.get("VIDEO_SUMMARY_HOME") or str(Path.cwd())


def extract_bvid(url: str) -> str:
    match = BVID_RE.search(url)
    if not match:
        raise SystemExit("ERROR: URL does not contain a Bilibili BV id")
    return match.group(0)


def resolve_source_url(url: str) -> str:
    """Resolve a b23.tv short URL while leaving normal Bilibili URLs untouched."""
    if BVID_RE.search(url):
        return url
    host = (urlparse(url).hostname or "").lower()
    if host not in {"b23.tv", "www.b23.tv"}:
        raise SystemExit("ERROR: expected a bilibili.com or b23.tv URL")
    httpx = load_httpx()
    headers = {"User-Agent": "Mozilla/5.0 (compatible; process-bilibili-course/1.0)"}
    with httpx.Client(headers=headers, follow_redirects=True, timeout=60) as client:
        response = client.get(url)
        response.raise_for_status()
        resolved = str(response.url)
    extract_bvid(resolved)
    return resolved


def requested_page(url: str) -> int | None:
    values = parse_qs(urlparse(url).query).get("p")
    return int(values[0]) if values and values[0].isdigit() else None


def load_httpx():
    try:
        import httpx
    except ImportError as exc:
        raise SystemExit("ERROR: httpx is unavailable; run: python -m pip install httpx") from exc
    return httpx


def fetch_view(url: str):
    httpx = load_httpx()
    bvid = extract_bvid(url)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/138 Safari/537.36",
        "Referer": f"https://www.bilibili.com/video/{bvid}",
    }
    client = httpx.Client(headers=headers, follow_redirects=True, timeout=120)
    response = client.get("https://api.bilibili.com/x/web-interface/view", params={"bvid": bvid})
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != 0:
        raise SystemExit(f"ERROR: Bilibili view API {payload.get('code')}: {payload.get('message')}")
    return client, bvid, payload["data"]


def choose_pages(data: dict, start: int | None, end: int | None) -> list[dict]:
    pages = sorted(data.get("pages", []), key=lambda item: int(item["page"]))
    if start is not None:
        pages = [item for item in pages if int(item["page"]) >= start]
    if end is not None:
        pages = [item for item in pages if int(item["page"]) <= end]
    if not pages:
        raise SystemExit("ERROR: no pages selected")
    return pages


def mark_compilations(pages: list[dict]) -> list[dict]:
    durations = [int(item.get("duration") or 0) for item in pages if int(item.get("duration") or 0) > 0]
    # Exclude the single longest item from the baseline so a two-part series
    # containing one normal lesson plus one compilation is still detectable.
    baseline_values = sorted(durations)[:-1] if len(durations) > 1 else durations
    median = statistics.median(baseline_values) if baseline_values else 0
    last_page = max(int(item["page"]) for item in pages)
    result = []
    for item in pages:
        row = dict(item)
        duration = int(row.get("duration") or 0)
        suspicious_title = bool(COMPILATION_RE.search(row.get("part") or ""))
        much_longer = bool(median and duration > max(3600, median * 2.5))
        extreme = bool(median and duration > max(7200, median * 4))
        row["review_required"] = len(pages) > 1 and (extreme or (int(row["page"]) == last_page and suspicious_title and much_longer))
        row["review_reason"] = "possible compilation of earlier parts" if row["review_required"] else ""
        result.append(row)
    return result


def layout(workspace: Path, course: str) -> dict[str, Path]:
    course_root = workspace / "课程资料" / course
    work_root = workspace / "后台处理文件" / course
    paths = {
        "entry": course_root / "00-课程入口",
        "notes": course_root / "01-详细笔记",
        "transcripts": course_root / "02-逐字稿",
        "audio": course_root / "03-音频",
        "timestamped": work_root / "时间戳逐字稿",
        "segments": work_root / "分段数据",
        "metadata": work_root / "元数据",
        "cache": work_root / "原始缓存",
        "manifest": work_root / "course-manifest.json",
    }
    for key, path in paths.items():
        if key != "manifest":
            path.mkdir(parents=True, exist_ok=True)
    return paths


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def build_manifest(source_url: str, resolved_url: str, bvid: str, data: dict, pages: list[dict], course: str, path: Path) -> dict:
    existing = {}
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8-sig"))
    prior = {int(item["page"]): item for item in existing.get("episodes", [])}
    episodes = []
    for page in pages:
        number = int(page["page"])
        old = prior.get(number, {})
        episodes.append({
            "page": number,
            "cid": int(page["cid"]),
            "title": page.get("part") or f"第 {number} 集",
            "duration": int(page.get("duration") or 0),
            "review_required": bool(page.get("review_required")),
            "review_reason": page.get("review_reason") or "",
            "state": old.get("state", "pending"),
            "detail": old.get("detail", ""),
        })
    manifest = {
        "schema": 1,
        "source_url": source_url,
        "resolved_url": resolved_url,
        "bvid": bvid,
        "course": course,
        "video_title": data.get("title") or course,
        "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "episodes": episodes,
    }
    atomic_json(path, manifest)
    return manifest


def update_episode(manifest: dict, path: Path, page: int, state: str, detail: str = "") -> None:
    for item in manifest["episodes"]:
        if int(item["page"]) == page:
            item["state"] = state
            item["detail"] = detail
            break
    manifest["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    atomic_json(path, manifest)


def resolve_ffmpeg(args, workspace: Path) -> Path:
    candidates = []
    if args.ffmpeg:
        candidates.append(Path(args.ffmpeg).expanduser())
    if os.environ.get("FFMPEG_PATH"):
        candidates.append(Path(os.environ["FFMPEG_PATH"]).expanduser())
    if shutil.which("ffmpeg"):
        candidates.append(Path(shutil.which("ffmpeg")))
    candidates.extend([
        workspace / "tools" / "ffmpeg.exe",
        workspace / "转写工具" / "runtime" / "ffmpeg.exe",
    ])
    for value in candidates:
        if value.exists():
            return value.resolve()
    raise SystemExit(
        "ERROR: FFmpeg not found. Install ffmpeg, add it to PATH, set FFMPEG_PATH, "
        "or pass --ffmpeg <path>."
    )


def run_pipeline(args, client, bvid: str, data: dict, pages: list[dict], paths: dict[str, Path], manifest: dict) -> None:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise SystemExit("ERROR: faster-whisper unavailable; run: python -m pip install faster-whisper") from exc

    ffmpeg = resolve_ffmpeg(args, Path(args.workspace).resolve())
    configured_model_root = args.model_root or os.environ.get("FASTER_WHISPER_MODEL_ROOT")
    model_root = Path(configured_model_root).expanduser() if configured_model_root else Path(args.workspace).resolve() / ".models" / "faster-whisper"
    model_root.mkdir(parents=True, exist_ok=True)
    model = WhisperModel(
        args.model,
        device=args.device,
        compute_type=args.compute_type,
        download_root=str(model_root),
    )
    manifest_rows = {int(item["page"]): item for item in manifest["episodes"]}

    for page in pages:
        number = int(page["page"])
        episode_id = f"{number:02d}"
        record = manifest_rows[number]
        if record["review_required"] and not args.include_review:
            update_episode(manifest, paths["manifest"], number, "skipped_review", record["review_reason"])
            print(f"EP{episode_id} SKIP_REVIEW {record['review_reason']}", flush=True)
            continue

        transcript_path = paths["transcripts"] / f"{episode_id}-transcript.txt"
        audio_path = paths["audio"] / f"{episode_id}-audio.mp3"
        if not args.force and transcript_path.exists() and transcript_path.stat().st_size > 100 and audio_path.exists():
            update_episode(manifest, paths["manifest"], number, "transcribed", "existing valid output")
            print(f"EP{episode_id} SKIP_EXISTING", flush=True)
            continue

        cache_dir = paths["cache"] / episode_id
        cache_dir.mkdir(parents=True, exist_ok=True)
        source_path = cache_dir / f"{episode_id}-audio-source.m4s"
        print(f"EP{episode_id} DOWNLOAD {record['title']}", flush=True)
        play = client.get(
            "https://api.bilibili.com/x/player/playurl",
            params={"bvid": bvid, "cid": int(page["cid"]), "fnval": 16, "qn": 64},
        ).json()
        if play.get("code") != 0:
            update_episode(manifest, paths["manifest"], number, "access_error", str(play.get("message") or play.get("code")))
            print(f"EP{episode_id} ACCESS_ERROR {play.get('message')}", flush=True)
            continue
        options = play.get("data", {}).get("dash", {}).get("audio", [])
        if not options:
            update_episode(manifest, paths["manifest"], number, "access_error", "no public audio stream")
            print(f"EP{episode_id} ACCESS_ERROR no public audio stream", flush=True)
            continue
        selected = min(options, key=lambda value: int(value.get("bandwidth") or 0))
        media_url = selected.get("baseUrl") or selected.get("base_url")
        with client.stream("GET", media_url) as response:
            response.raise_for_status()
            with source_path.open("wb") as handle:
                for chunk in response.iter_bytes(1024 * 1024):
                    handle.write(chunk)
        update_episode(manifest, paths["manifest"], number, "downloaded", "temporary audio stream downloaded")

        subprocess.run([
            str(ffmpeg), "-y", "-loglevel", "error", "-i", str(source_path), "-vn",
            "-ac", "1", "-ar", "16000", "-c:a", "mp3", "-b:a", "48k", str(audio_path),
        ], check=True)
        if not audio_path.exists() or audio_path.stat().st_size < 10_000:
            update_episode(manifest, paths["manifest"], number, "conversion_error", "MP3 missing or unexpectedly small")
            continue
        source_path.unlink(missing_ok=True)
        update_episode(manifest, paths["manifest"], number, "converted", "compressed MP3 verified; temporary stream deleted")

        language = None if args.language == "auto" else args.language
        prompt = f"{data.get('title', '')}；{record['title']}；课程逐字稿，保留专业术语和专有名词。"
        print(f"EP{episode_id} TRANSCRIBE", flush=True)
        segments, info = model.transcribe(
            str(audio_path), language=language, beam_size=3, vad_filter=True,
            condition_on_previous_text=True, initial_prompt=prompt,
        )
        rows = [{"start": segment.start, "end": segment.end, "text": segment.text.strip()}
                for segment in segments if segment.text.strip()]
        text = "\n".join(row["text"] for row in rows).strip()
        if len(text) < 100 and int(record["duration"]) >= 30:
            retry_segments, retry_info = model.transcribe(
                str(audio_path), language=language, beam_size=5, vad_filter=False,
                condition_on_previous_text=True, initial_prompt=prompt,
            )
            retry_rows = [{"start": segment.start, "end": segment.end, "text": segment.text.strip()}
                          for segment in retry_segments if segment.text.strip()]
            retry_text = "\n".join(row["text"] for row in retry_rows).strip()
            if len(retry_text) > len(text):
                rows, text, info = retry_rows, retry_text, retry_info

        transcript_path.write_text(text + "\n", encoding="utf-8")
        (paths["timestamped"] / f"{episode_id}-transcript-timestamped.md").write_text(
            "\n\n".join(f"[{stamp(row['start'])}] {row['text']}" for row in rows) + "\n", encoding="utf-8"
        )
        atomic_json(paths["segments"] / f"{episode_id}-segments.json", {
            "episode": number, "language": info.language, "duration": info.duration, "segments": rows,
        })
        atomic_json(paths["metadata"] / f"{episode_id}-metadata.json", {
            "bvid": bvid, "page": number, "cid": int(page["cid"]), "title": record["title"],
            "declared_duration": int(record["duration"]), "transcribed_duration": info.duration,
        })
        if int(record["duration"]) < 30 or len(text) < 100:
            update_episode(manifest, paths["manifest"], number, "abnormal_short", f"duration={record['duration']}s chars={len(text)}")
            print(f"EP{episode_id} ABNORMAL_SHORT", flush=True)
        else:
            update_episode(manifest, paths["manifest"], number, "transcribed", f"segments={len(rows)} chars={len(text)}")
            print(f"EP{episode_id} DONE segments={len(rows)}", flush=True)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("command", choices=("inspect", "run"))
    result.add_argument("--url", required=True)
    result.add_argument("--workspace", default=default_workspace())
    result.add_argument("--course-name")
    result.add_argument("--start", type=int)
    result.add_argument("--end", type=int)
    result.add_argument("--language", choices=("auto", "zh", "en"), default="auto")
    result.add_argument("--ffmpeg")
    result.add_argument("--model-root")
    result.add_argument("--model", default=os.environ.get("FASTER_WHISPER_MODEL", "small"))
    result.add_argument("--device", default=os.environ.get("FASTER_WHISPER_DEVICE", "cpu"))
    result.add_argument("--compute-type", default=os.environ.get("FASTER_WHISPER_COMPUTE_TYPE", "int8"))
    result.add_argument("--include-review", action="store_true")
    result.add_argument("--force", action="store_true")
    return result


def main() -> None:
    args = parser().parse_args()
    resolved_url = resolve_source_url(args.url)
    client, bvid, data = fetch_view(resolved_url)
    try:
        pages = mark_compilations(choose_pages(data, args.start, args.end))
        course = safe_name(args.course_name or data.get("title") or bvid)
        inspection = {
            "source_url": args.url,
            "resolved_url": resolved_url,
            "bvid": bvid,
            "course": course,
            "selected_p_from_url": requested_page(resolved_url),
            "total_parts": len(data.get("pages", [])),
            "selected_parts": len(pages),
            "episodes": [{key: item.get(key) for key in ("page", "cid", "part", "duration", "review_required", "review_reason")}
                         for item in pages],
        }
        if args.command == "inspect":
            print(json.dumps(inspection, ensure_ascii=False, indent=2))
            return
        workspace = Path(args.workspace).expanduser().resolve()
        paths = layout(workspace, course)
        manifest = build_manifest(args.url, resolved_url, bvid, data, pages, course, paths["manifest"])
        run_pipeline(args, client, bvid, data, pages, paths, manifest)
        print(json.dumps({"manifest": str(paths["manifest"]), "course": course}, ensure_ascii=False))
    finally:
        client.close()


if __name__ == "__main__":
    main()
