---
name: process-bilibili-course
description: Download and process public Bilibili videos or multi-part courses into compressed audio, local faster-whisper transcripts, faithful detailed per-episode notes, a linked course index, and a separate concise collection. Use when the user provides a bilibili.com or b23.tv link and asks to download, transcribe, summarize, take notes, process every episode, resume an interrupted course, or organize the resulting course materials.
---

# Process Bilibili Course

Turn a Bilibili link into an idempotent, resumable course archive. Default to compressed audio and text only; delete temporary media streams after verified conversion.

## Required defaults

- Process every unique part unless the user explicitly selects a range.
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

Run `python scripts/bilibili_pipeline.py inspect --url <URL> --workspace <workspace>` on the URL. The script resolves `b23.tv` links automatically. Build a manifest from Bilibili `page`, `cid`, title, and duration. Do not infer episode order from cache folder names.

Review the inspection output for:

- inaccessible or login-required content;
- a final “全集/合集/完整版” part that duplicates earlier parts;
- unexpectedly short parts;
- ambiguous course titles;
- a URL pointing to one selected `p=` while the containing series has many parts.

Default to the entire series. Ask only when access requires user cookies, the desired course name is materially ambiguous, or a suspected duplicate cannot be decided safely.

### 3. Download and transcribe

Run `python scripts/bilibili_pipeline.py run --url <URL> --workspace <workspace>`. Prefer Bilibili's public metadata/playurl API. Download the smallest suitable audio stream, convert it to mono 16 kHz 48 kbps MP3, verify the MP3, then delete the temporary `.m4s` file.

The script writes the transcript, timestamped transcript, segments, metadata, compressed audio, and `course-manifest.json`. It checkpoints after each episode and skips valid existing output.

If public access fails, follow the cookie fallback in `failure-handling.md`; never extract browser cookies without permission.

### 4. Write detailed notes in internal batches

Process transcripts in batches of at most ten episodes to control context. Do not pause for approval between batches unless the user asks for review. Apply `note-standard.md` consistently from the first episode to the last.

For every episode:

1. Read the complete transcript.
2. Preserve every distinct argument, example, number, comparison, causal chain, and conclusion.
3. Convert spoken repetition into smooth prose without deleting information.
4. Link the title to `../02-逐字稿/NN-transcript.txt`.
5. Write `课程资料/<course>/01-详细笔记/NN-notes.md`.
6. Mark the episode `notes_done` with `scripts/course_utils.py mark`.

For abnormal short source content, write an explicit exception note instead of inventing content.

### 5. Create navigation and the concise collection

Run `scripts/course_utils.py index` after notes exist. Then create `<course>-精炼笔记合集.md` in `00-课程入口` using the separate concise-note rules in `note-standard.md`.

The detailed notes are cleaned full-content records. The concise collection is the only place where aggressive compression is allowed.

### 6. Validate and report

Run `scripts/course_utils.py validate`. Do not declare completion until:

- every unique expected part has audio, transcript, and note, or an explicit exception;
- suspected compilation parts are recorded as skipped or intentionally included;
- all Markdown links resolve;
- no temporary media stream remains after successful conversion;
- the manifest contains a terminal state for every selected part;
- detailed notes do not become systematically shorter or omit examples in later episodes.

Report completed, skipped, abnormal, and pending counts. Link the course index in the final response.

## Interruption behavior

When resuming, read `course-manifest.json`, verify files on disk, and continue from the earliest incomplete state. Treat stale PID files as history, not proof that a process is running. If the computer slept or Codex closed, explain that execution stopped and resume from the checkpoint.
