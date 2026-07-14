#!/usr/bin/env python3
"""Inspect, download, convert, and locally transcribe public Bilibili or Xiaohongshu videos."""

from __future__ import annotations

import argparse
import html
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
URL_RE = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+", re.IGNORECASE)
XHS_ITEM_RE = re.compile(r"/discovery/item/([0-9A-Fa-f]+)")
JSON_LD_RE = re.compile(
    r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)
COMPILATION_RE = re.compile(r"全集|合集|完整版|一口气|全\d+[集讲节]|完整课程")
INVALID_WINDOWS = re.compile(r'[<>:"/\\|?*]+')
BILIBILI_HOSTS = {"bilibili.com", "www.bilibili.com", "m.bilibili.com", "b23.tv", "www.b23.tv"}
XHS_HOSTS = {"xhslink.com", "www.xhslink.com", "xiaohongshu.com", "www.xiaohongshu.com"}


def stamp(seconds: float) -> str:
    total = round(seconds * 1000)
    hours, rem = divmod(total, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def safe_name(value: str) -> str:
    value = INVALID_WINDOWS.sub("-", value).strip(" .-")
    value = re.sub(r"\s+", " ", value)
    return value[:80] or "视频课程"


def default_workspace() -> str:
    """Use an explicit environment override or the current working directory."""
    return os.environ.get("VIDEO_SUMMARY_HOME") or str(Path.cwd())


def extract_bvid(url: str) -> str:
    match = BVID_RE.search(url)
    if not match:
        raise SystemExit("ERROR: URL does not contain a Bilibili BV id")
    return match.group(0)


def extract_input_url(value: str) -> str:
    """Accept a bare URL or common share text containing one URL."""
    match = URL_RE.search(value)
    if not match:
        raise SystemExit("ERROR: input does not contain an HTTP(S) URL")
    return match.group(0).rstrip(".,;:!?")


def hostname(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def detect_platform(url: str) -> str:
    host = hostname(url)
    if host in BILIBILI_HOSTS or BVID_RE.search(url):
        return "bilibili"
    if host in XHS_HOSTS:
        return "xiaohongshu"
    raise SystemExit("ERROR: expected a bilibili.com, b23.tv, xiaohongshu.com, or xhslink.com URL")


def manifest_source_url(input_url: str, platform: str, canonical_url: str) -> str:
    """Avoid persisting Xiaohongshu share-token query parameters."""
    if platform == "xiaohongshu":
        return canonical_url
    return input_url


def resolve_source_url(url: str) -> str:
    """Resolve supported short URLs and validate the final public platform URL."""
    url = extract_input_url(url)
    platform = detect_platform(url)
    host = hostname(url)
    if platform == "bilibili" and BVID_RE.search(url):
        return url
    if platform == "xiaohongshu" and host in {"xiaohongshu.com", "www.xiaohongshu.com"}:
        if not XHS_ITEM_RE.search(url):
            raise SystemExit("ERROR: Xiaohongshu URL does not contain a discovery item id")
        return url
    httpx = load_httpx()
    headers = {"User-Agent": "Mozilla/5.0 (compatible; process-bilibili-course/1.1)"}
    with httpx.Client(headers=headers, follow_redirects=True, timeout=60) as client:
        response = client.get(url)
        response.raise_for_status()
        resolved = str(response.url)
    if platform == "bilibili":
        extract_bvid(resolved)
    elif hostname(resolved) not in {"xiaohongshu.com", "www.xiaohongshu.com"} or not XHS_ITEM_RE.search(resolved):
        raise SystemExit("ERROR: xhslink.com did not resolve to a Xiaohongshu discovery item")
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


def fetch_bilibili_view(url: str):
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


def parse_duration(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000 if number > 10_000 else number
    text = str(value).strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return parse_duration(float(text))
    if re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", text):
        parts = [int(part) for part in text.split(":")]
        if len(parts) == 2:
            return float(parts[0] * 60 + parts[1])
        return float(parts[0] * 3600 + parts[1] * 60 + parts[2])
    iso = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", text, re.IGNORECASE)
    if iso:
        hours, minutes, seconds = iso.groups()
        return float(hours or 0) * 3600 + float(minutes or 0) * 60 + float(seconds or 0)
    return 0.0


def iter_json_ld(source: str):
    for match in JSON_LD_RE.finditer(source):
        try:
            value = json.loads(html.unescape(match.group(1)).strip())
        except (json.JSONDecodeError, TypeError):
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict):
                yield item


def decode_json_string(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value.replace(r"\u002F", "/").replace(r"\/", "/")


def fetch_xiaohongshu_view(url: str):
    httpx = load_httpx()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/138 Safari/537.36",
        "Referer": "https://www.xiaohongshu.com/",
    }
    client = httpx.Client(headers=headers, follow_redirects=True, timeout=120)
    response = client.get(url)
    response.raise_for_status()
    final_url = str(response.url)
    item_match = XHS_ITEM_RE.search(final_url)
    if hostname(final_url) not in {"xiaohongshu.com", "www.xiaohongshu.com"} or not item_match:
        client.close()
        raise SystemExit("ERROR: Xiaohongshu share link did not resolve to a public discovery item")
    item_id = item_match.group(1)
    source = response.text
    title = ""
    media_url = ""
    duration = 0.0
    for item in iter_json_ld(source):
        kind = item.get("@type")
        if kind not in {"VideoObject", "SocialMediaPosting", None}:
            continue
        title = title or str(item.get("name") or item.get("headline") or "").strip()
        media_url = media_url or str(item.get("contentUrl") or item.get("embedUrl") or "").strip()
        duration = duration or parse_duration(item.get("duration"))
    if not title:
        title_match = TITLE_RE.search(source)
        if title_match:
            title = html.unescape(re.sub(r"<[^>]+>", "", title_match.group(1))).strip()
    title = re.sub(r"\s*-\s*小红书\s*$", "", title).strip() or f"小红书视频-{item_id}"
    if not media_url:
        media_match = re.search(r'"masterUrl":"([^\"]+)"', source)
        if media_match:
            media_url = decode_json_string(media_match.group(1))
    if not duration:
        duration_match = re.search(r'"videoDuration":(\d+(?:\.\d+)?)', source)
        if duration_match:
            duration = parse_duration(float(duration_match.group(1)))
    if not media_url:
        client.close()
        raise SystemExit("ERROR: no public Xiaohongshu video stream found; the note may be image-only or login-gated")
    media_url = html.unescape(media_url)
    media_host = hostname(media_url)
    if not (media_host == "xhscdn.com" or media_host.endswith(".xhscdn.com")):
        client.close()
        raise SystemExit("ERROR: Xiaohongshu page returned an unexpected media host")
    canonical_url = f"https://www.xiaohongshu.com/discovery/item/{item_id}"
    client.headers["Referer"] = canonical_url
    data = {
        "platform": "xiaohongshu",
        "source_id": item_id,
        "title": title,
        "canonical_url": canonical_url,
        "pages": [{
            "page": 1,
            "cid": None,
            "part": title,
            "duration": int(round(duration)),
            "media_url": media_url,
            "review_required": False,
            "review_reason": "",
        }],
    }
    return client, item_id, data


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


def build_manifest(
    source_url: str,
    resolved_url: str,
    platform: str,
    source_id: str,
    data: dict,
    pages: list[dict],
    course: str,
    path: Path,
) -> dict:
    existing = {}
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8-sig"))
    prior = {int(item["page"]): item for item in existing.get("episodes", [])}
    episodes = []
    for page in pages:
        number = int(page["page"])
        old = prior.get(number, {})
        episode = {
            "page": number,
            "title": page.get("part") or f"第 {number} 集",
            "duration": int(page.get("duration") or 0),
            "review_required": bool(page.get("review_required")),
            "review_reason": page.get("review_reason") or "",
            "state": old.get("state", "pending"),
            "detail": old.get("detail", ""),
        }
        if page.get("cid") is not None:
            episode["cid"] = int(page["cid"])
        episodes.append(episode)
    manifest = {
        "schema": 2,
        "platform": platform,
        "source_url": source_url,
        "resolved_url": resolved_url,
        "source_id": source_id,
        "course": course,
        "video_title": data.get("title") or course,
        "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "episodes": episodes,
    }
    if platform == "bilibili":
        manifest["bvid"] = source_id
    elif platform == "xiaohongshu":
        manifest["item_id"] = source_id
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


def run_pipeline(
    args,
    client,
    platform: str,
    source_id: str,
    data: dict,
    pages: list[dict],
    paths: dict[str, Path],
    manifest: dict,
) -> None:
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
        source_path = cache_dir / (
            f"{episode_id}-audio-source.m4s" if platform == "bilibili" else f"{episode_id}-video-source.mp4"
        )
        print(f"EP{episode_id} DOWNLOAD {record['title']}", flush=True)
        try:
            if platform == "bilibili":
                play = client.get(
                    "https://api.bilibili.com/x/player/playurl",
                    params={"bvid": source_id, "cid": int(page["cid"]), "fnval": 16, "qn": 64},
                ).json()
                if play.get("code") != 0:
                    detail = str(play.get("message") or play.get("code"))
                    update_episode(manifest, paths["manifest"], number, "access_error", detail)
                    print(f"EP{episode_id} ACCESS_ERROR {detail}", flush=True)
                    continue
                options = play.get("data", {}).get("dash", {}).get("audio", [])
                if not options:
                    update_episode(manifest, paths["manifest"], number, "access_error", "no public audio stream")
                    print(f"EP{episode_id} ACCESS_ERROR no public audio stream", flush=True)
                    continue
                selected = min(options, key=lambda value: int(value.get("bandwidth") or 0))
                media_url = selected.get("baseUrl") or selected.get("base_url")
            else:
                media_url = page["media_url"]
            with client.stream("GET", media_url) as response:
                response.raise_for_status()
                with source_path.open("wb") as handle:
                    for chunk in response.iter_bytes(1024 * 1024):
                        handle.write(chunk)
        except Exception as exc:
            detail = f"{type(exc).__name__}: public media download failed"
            update_episode(manifest, paths["manifest"], number, "access_error", detail)
            print(f"EP{episode_id} ACCESS_ERROR {detail}", flush=True)
            continue
        update_episode(manifest, paths["manifest"], number, "downloaded", "temporary public media downloaded")

        subprocess.run([
            str(ffmpeg), "-y", "-loglevel", "error", "-i", str(source_path), "-vn",
            "-ac", "1", "-ar", "16000", "-c:a", "mp3", "-b:a", "48k", str(audio_path),
        ], check=True)
        if not audio_path.exists() or audio_path.stat().st_size < 10_000:
            update_episode(manifest, paths["manifest"], number, "conversion_error", "MP3 missing or unexpectedly small")
            continue
        verification = subprocess.run(
            [str(ffmpeg), "-v", "error", "-i", str(audio_path), "-f", "null", "-"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        if verification.returncode != 0:
            update_episode(manifest, paths["manifest"], number, "conversion_error", "MP3 verification failed")
            continue
        source_path.unlink(missing_ok=True)
        update_episode(manifest, paths["manifest"], number, "converted", "compressed MP3 verified; temporary media deleted")

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
        metadata = {
            "platform": platform,
            "source_id": source_id,
            "page": number,
            "title": record["title"],
            "declared_duration": int(record["duration"]),
            "transcribed_duration": info.duration,
            "model": args.model,
        }
        if platform == "bilibili":
            metadata.update({"bvid": source_id, "cid": int(page["cid"])})
        else:
            metadata["item_id"] = source_id
        atomic_json(paths["metadata"] / f"{episode_id}-metadata.json", metadata)
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
    input_url = extract_input_url(args.url)
    source_url = input_url
    resolved_url = resolve_source_url(input_url)
    platform = detect_platform(resolved_url)
    if platform == "bilibili":
        client, source_id, data = fetch_bilibili_view(resolved_url)
    else:
        client, source_id, data = fetch_xiaohongshu_view(resolved_url)
        resolved_url = data["canonical_url"]
        source_url = manifest_source_url(input_url, platform, resolved_url)
    try:
        pages = mark_compilations(choose_pages(data, args.start, args.end))
        course = safe_name(args.course_name or data.get("title") or source_id)
        inspection = {
            "platform": platform,
            "source_url": source_url,
            "resolved_url": resolved_url,
            "source_id": source_id,
            "course": course,
            "total_parts": len(data.get("pages", [])),
            "selected_parts": len(pages),
            "episodes": [{key: item.get(key) for key in ("page", "cid", "part", "duration", "review_required", "review_reason")}
                         for item in pages],
        }
        if platform == "bilibili":
            inspection.update({"bvid": source_id, "selected_p_from_url": requested_page(resolved_url)})
        else:
            inspection["item_id"] = source_id
        if args.command == "inspect":
            print(json.dumps(inspection, ensure_ascii=False, indent=2))
            return
        workspace = Path(args.workspace).expanduser().resolve()
        paths = layout(workspace, course)
        manifest = build_manifest(
            source_url, resolved_url, platform, source_id, data, pages, course, paths["manifest"]
        )
        run_pipeline(args, client, platform, source_id, data, pages, paths, manifest)
        print(json.dumps({"manifest": str(paths["manifest"]), "course": course, "platform": platform}, ensure_ascii=False))
    finally:
        client.close()


if __name__ == "__main__":
    main()
