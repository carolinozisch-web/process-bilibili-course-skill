# Saved-Item Knowledge Base

The saved-item layer is local, single-user, and review-first. Source Markdown, audio, transcripts, metadata, and manifests remain authoritative; SQLite adds workflow state and search.

## Lifecycle

New items follow `discovered -> transcribed -> triage_ready -> approved -> curated`. `deferred`, `rejected`, and `error` require an explicit retry or review action. Existing course archives migrate to `legacy_imported` with `review_decision=legacy_migration`; migration is provenance, never user approval.

Only `approved` or `curated` sources may be linked to knowledge cards. AI may suggest duplicate cards or sources but must not merge, delete, approve, or reject automatically.

## Triage and curation

- `triage.summary_50` contains `①`, `②`, and `③` and is at most 50 characters.
- Evidence timestamps are stored separately from the short summary.
- Basic mode samples the complete transcript and is labeled `basic`.
- AI mode chunks long transcripts, extracts evidence per chunk, and reduces the candidates.
- A reviewed source may produce zero to five method cards. Zero is valid when the source contains no reusable method.
- Cards keep an actionable title, use case, steps, constraints, common questions, symptoms, keywords, tags, and source time range.

## Jobs

Transcription and AI curation use `processing_jobs`. Only one worker runs locally. A process restart moves stale `running` jobs back to `queued`; the underlying course manifest provides file-level resume behavior. Re-importing a URL must not create another active job for the same source and job type.

## Search

SQLite FTS5 is the first-stage index. Literal `LIKE` fallback preserves useful Chinese matching when tokenization is weak. Optional AI may expand a query and synthesize a cited answer from retrieved results; it does not replace retrieval. Search must include timestamps and original URLs when available and say `没有直接答案` when no evidence is found.

## Model settings and privacy

The optional client accepts an OpenAI-compatible Chat Completions base URL, model name, and bearer API key. The key may come from `VIDEO_KB_LLM_API_KEY` or the current web process. Never persist it. Base URL and model may be shown in the UI; the key must not be returned, logged, placed in job payloads, or committed.

The web server binds to `127.0.0.1`, accepts same-host requests only, limits JSON request size, and serves only bundled web assets. The workflow does not scrape private collections, read browser cookies, or bypass login, CAPTCHA, payment, region, membership, or DRM controls.

## Migration and backups

`schema_meta.schema_version` controls database upgrades. Before upgrading an unversioned knowledge database, copy it to `知识库/backups/`. Migration must preserve source counts and file paths. It may repair legacy workflow semantics but must not move, rewrite, or re-transcribe source files.
