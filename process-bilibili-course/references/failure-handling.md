# Failure handling

## Access and download

- Try the public Bilibili metadata and playurl APIs first.
- Resolve `b23.tv` to its final Bilibili video URL before metadata inspection. The bundled pipeline does this automatically.
- If content requires login, region access, membership, or anti-bot cookies, stop and ask permission to use cookies from an installed Edge or Chrome profile.
- Never assume Firefox is installed.
- Do not paste, print, or store raw cookie values in notes or logs.

## Numbering and missing parts

- Build the manifest from `page`, `cid`, part title, and declared duration.
- Do not equate cache position with course lesson number.
- If a part is absent from the API response, report it before processing later parts.

## Abnormally short content

Flag a part when declared duration is under 30 seconds, converted audio is unexpectedly small, or a normal-duration part produces fewer than 100 transcript characters.

Procedure:

1. Redownload once.
2. Reconvert with FFmpeg.
3. Retranscribe once with less aggressive VAD.
4. If still short, retain the evidence and create an exception note stating the actual duration/content.

Never expand a four-second source into a fabricated lesson.

## Duplicate compilations

Treat titles containing `全集`, `合集`, `完整版`, `一口气`, or similar wording as possible compilations when they occur inside a multi-part series and are much longer than typical parts.

Compare:

- title and duration;
- whether it is the final part;
- transcript overlap with earlier parts when available.

Skip a clear compilation and record the reason. If evidence is ambiguous, ask the user before spending hours transcribing it.

## Transcription quality

- Keep raw transcripts unchanged after creation.
- Use course title and part title as the initial prompt.
- Auto-detect language for mixed or unknown courses.
- Correct proper nouns only in notes and only when confident.
- Keep low-confidence names cautious rather than silently inventing a replacement.

## Interrupted runs

- Update the manifest after metadata inspection, download, conversion, transcription, note creation, index creation, and validation.
- On resume, trust both manifest state and actual file validity; repair disagreement.
- A PID file can be stale. Verify a live process before claiming work continues.
- Windows sleep, shutdown, and closing Codex stop local execution. Resume from the saved state after wake/reopen.

## Token and cost control

- Use local faster-whisper by default.
- Read one transcript only when writing or checking its note.
- Work in batches of at most ten episodes.
- Do not repeatedly load the complete course collection during per-episode work.
- Build indexes mechanically and validate links with scripts.
