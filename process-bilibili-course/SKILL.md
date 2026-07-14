---
name: process-bilibili-course
description: Download and process public Bilibili or Xiaohongshu videos into compressed audio, local faster-whisper transcripts, faithful detailed notes, linked indexes, and a separate concise collection. Use when the user provides bilibili.com, b23.tv, xiaohongshu.com, or xhslink.com links or share text and asks to download, transcribe, summarize, take notes, process every episode, resume interrupted work, or organize the resulting course materials.
---

# Process Public Video Courses

Turn a public Bilibili course or public Xiaohongshu video into an idempotent, resumable archive. Default to compressed audio and text only; delete temporary media after verified conversion.

## Required defaults

- Process every unique Bilibili part unless the user explicitly selects a range. Treat a Xiaohongshu note as one episode.
- Use local `faster-whisper small` to avoid transcription API cost.
- Auto-detect Chinese versus English unless the language is obvious from metadata.
- Create faithful, detailed notes for each episode and a separate concise course collection.
- Preserve raw transcripts. Correct proper nouns in notes only when confident.
- Save progress after every episode. Resume existing work rather than restarting.
- Keep one course per directory. Never start or resume another course implicitly.
- Do not promise work during Windows sleep, shutdown, or while Codex is closed.

## Read the references

- Read [references/output-layout.md](references/output-layout.md) before creating or moving course files.
- Read [references/note-standard.md](references/note-standard.md) before writing any notes or combined collection.
- Read [references/failure-handling.md](references/failure-handling.md) when inspection, download, transcription, numbering, duplication, or validation is abnormal.

## Workflow

### 1. Resolve the environment

Use the user-named workspace. Otherwise use `VIDEO_SUMMARY_HOME` when it is set, then fall back to the current working directory.

Require these local dependencies:

- Python 3.10 or newer
- Python packages `httpx` and `faster-whisper`
- FFmpeg, resolved from `--ffmpeg`, `FFMPEG_PATH`, the system `PATH`, or a workspace fallback
- A writable model cache, defaulting to `<workspace>/.models/faster-whisper`

Install Python dependencies when missing with `python -m pip install httpx faster-whisper`. Do not require an OpenAI API key.

### 2. Inspect before downloading

Run `python scripts/video_pipeline.py inspect --url <URL-OR-SHARE-TEXT> --workspace <workspace>`. The script accepts bare links or common share text and resolves `b23.tv` and `xhslink.com` automatically.

For Bilibili, build the manifest from `page`, `cid`, title, and duration. Do not infer episode order from cache folder names. For Xiaohongshu, accept only a public video note with an exposed CDN video stream; record its item ID, canonical URL, title, and duration as one episode.

Review the inspection output for:

- inaccessible or login-required content;
- a final “全集/合集/完整版” part that duplicates earlier parts;
- unexpectedly short parts;
- ambiguous course titles;
- a URL pointing to one selected `p=` while the containing series has many parts.
- a Xiaohongshu page that is image-only, login-gated, deleted, expired, or missing a public video stream.

Default to the entire Bilibili series. Stop when access requires authentication, membership, payment, region bypass, DRM circumvention, or browser Cookie extraction. Ask only when the desired course name is materially ambiguous or a suspected duplicate cannot be decided safely.

### 3. Download and transcribe

Run `python scripts/video_pipeline.py run --url <URL-OR-SHARE-TEXT> --workspace <workspace>`. For Bilibili, prefer the public metadata and playurl APIs and choose the smallest suitable audio stream. For Xiaohongshu, use the public page's JSON-LD or embedded `masterUrl` video stream. Convert the source to mono 16 kHz 48 kbps MP3, decode-check the MP3, then delete the temporary `.m4s` or `.mp4` file.

The script writes the transcript, timestamped transcript, segments, metadata, compressed audio, and `course-manifest.json`. It checkpoints after each episode and skips valid existing output.

If public access fails, follow `failure-handling.md`. The bundled workflow must not extract browser Cookies or bypass access controls.

### 4. Write detailed notes in internal batches

Process transcripts in batches of at most ten episodes to control context. Do not pause for approval between batches unless the user asks for review. Apply `note-standard.md` consistently from the first episode to the last.

For every episode:

1. Read the complete transcript.
2. Preserve every distinct argument, example, number, comparison, causal chain, and conclusion.
3. Convert spoken repetition into smooth prose without deleting information.
4. Link the title to the corresponding `后台处理文件/<course>/时间戳逐字稿/NN-transcript-timestamped.md` using a valid relative path.
5. Write `课程资料/<course>/01-详细笔记/NN-notes.md`.
6. Mark the episode `notes_done` with `scripts/course_utils.py mark`.

For abnormal short source content, write an explicit exception note instead of inventing content.

### 5. Create navigation and the concise collection

Run `scripts/course_utils.py index` after notes exist. It must inspect the manifest before adding navigation. When more than one active unique episode exists, it adds “回到目录” at the top and bottom of every detailed note and adds “下一篇” when a later active episode exists. When only one active video exists, it adds no directory or next-note navigation and removes any previously generated navigation. It always links detailed-note headings to timestamped transcripts.

Then create `<course>-精炼笔记合集.md` in `00-课程入口` using the separate concise-note rules in `note-standard.md`.

The detailed notes are cleaned full-content records. The concise collection is the only place where aggressive compression is allowed.

### 6. Validate and report

Run `scripts/course_utils.py validate`. Do not declare completion until:

- every unique expected part has audio, transcript, and note, or an explicit exception;
- suspected compilation parts are recorded as skipped or intentionally included;
- all Markdown links resolve;
- no temporary media remains after successful conversion;
- the manifest contains a terminal state for every selected part;
- detailed notes do not become systematically shorter or omit examples in later episodes.

Report completed, skipped, abnormal, and pending counts. Link the course index in the final response.

## Interruption behavior

When resuming, read `course-manifest.json`, verify files on disk, and continue from the earliest incomplete state. Treat stale PID files as history, not proof that a process is running. If the computer slept or Codex closed, explain that execution stopped and resume from the checkpoint.
