---
name: process-bilibili-course
description: Process public Bilibili or Xiaohongshu courses and personal saved links with local transcription, faithful course notes, review-first triage, and a private SQLite knowledge base. Use when the user provides supported links or asks to download, transcribe, summarize, review, migrate, search, or organize saved video materials.
---

# Process Public Videos and Saved Knowledge

Use one of two modes:

- **Course mode** creates a complete, resumable course archive with transcripts, detailed notes, navigation, and a concise collection.
- **Saved-item mode** imports personal links into a local review queue, transcribes them, generates a short triage record, and waits for explicit approval before creating reusable knowledge cards.

Never extract browser cookies, bypass authentication or payment controls, or automatically delete a platform collection item.

## Saved-item mode

Read [references/knowledge-base.md](references/knowledge-base.md) before changing review, database, AI, or web behavior.

Start the local product interface with:

```text
python scripts/web_app.py --workspace <workspace>
```

The server binds only to `127.0.0.1`, chooses another local port when needed, resumes interrupted local jobs, and opens the browser. AI enrichment is optional; without API settings, the app creates a clearly labeled basic triage and supports manually confirmed method cards.

CLI equivalents:

```text
python scripts/favorite_pipeline.py import --workspace <workspace> --url <url> [--transcribe]
python scripts/favorite_pipeline.py triage --workspace <workspace> --source-id <id>
python scripts/favorite_pipeline.py queue --workspace <workspace>
python scripts/favorite_pipeline.py review --workspace <workspace> --source-id <id> --decision approve|defer|reject
python scripts/favorite_pipeline.py curate --workspace <workspace> --source-id <id> [--json <method-cards.json>]
python scripts/favorite_pipeline.py migrate-existing --workspace <workspace>
python scripts/favorite_pipeline.py export-today --workspace <workspace>
python scripts/favorite_pipeline.py search --workspace <workspace> --query <question>
```

Saved-item invariants:

- Never create or link a formal knowledge card before explicit approval.
- Read the complete transcript for triage; do not summarize only its beginning.
- Keep every raw source even when several sources support the same method card.
- Treat migrated archives as `legacy_imported`, not as newly reviewed material.
- Keep API keys in process memory or environment variables only. Never write them to files, SQLite, logs, jobs, or exports.
- A missing direct search answer is valid. Return related sources instead of inventing an answer.

## Course mode

### Required defaults

- Process every unique Bilibili part unless the user explicitly selects a range. Treat a Xiaohongshu note as one episode.
- Use local `faster-whisper small` and auto-detect Chinese versus English.
- Preserve compressed audio, raw transcripts, timestamped transcripts, metadata, detailed notes, and a separate concise collection.
- Save after every episode and resume existing work rather than restarting.
- Keep one course per directory. Never start or resume another course implicitly.

Read [references/output-layout.md](references/output-layout.md) before creating or moving files, [references/note-standard.md](references/note-standard.md) before writing notes, and [references/failure-handling.md](references/failure-handling.md) when access, download, transcription, numbering, or validation is abnormal.

### Inspect and process

Use the user-named workspace, then `VIDEO_SUMMARY_HOME`, then the current directory. Require Python 3.10+, `httpx`, `faster-whisper`, FFmpeg, and a writable model cache.

Inspect first:

```text
python scripts/video_pipeline.py inspect --url <URL-OR-SHARE-TEXT> --workspace <workspace>
```

Confirm public access, series structure, suspected compilation episodes, unusually short parts, and the selected `p=` value. Stop when access requires login, membership, payment, region bypass, DRM circumvention, or browser cookie extraction.

Run:

```text
python scripts/video_pipeline.py run --url <URL-OR-SHARE-TEXT> --workspace <workspace>
```

The pipeline chooses compact audio, converts it to mono 16 kHz 48 kbps MP3, verifies the result, removes temporary media, transcribes locally, and checkpoints each episode.

### Notes and validation

Read complete transcripts in batches of at most ten episodes. Preserve distinct arguments, examples, numbers, comparisons, causal chains, and conclusions. Link note headings to timestamped transcripts and mark each completed episode with `scripts/course_utils.py mark`.

Run `scripts/course_utils.py index` after notes exist, then create the separate concise collection. Finally run `scripts/course_utils.py validate`. Do not declare completion until expected audio, transcripts, notes, links, cleanup, and manifest states are verified or explicitly recorded as exceptions.

When resuming after sleep, shutdown, or interruption, inspect `course-manifest.json` and continue from the earliest incomplete state.
