# Saved-Item Knowledge Base

The saved-item layer is local, single-user, and review-first. Source Markdown, audio, transcripts, metadata, and manifests remain authoritative; SQLite adds workflow state and search.

## Lifecycle

New items follow `discovered -> transcribed -> triage_ready -> approved -> curated`. `deferred`, `rejected`, and `error` require an explicit retry or review action. Existing course archives migrate to `legacy_imported` with `review_decision=legacy_migration`; migration is provenance, never user approval.

Only `approved` or `curated` sources may be linked to knowledge cards. AI may suggest duplicate cards or sources but must not merge, delete, approve, or reject automatically.

## Triage and curation

- `triage.video_outline_json` stores an overview and logical sections with summaries, points, and evidence. Legacy short-summary and question fields remain for compatibility, not as the primary structure.
- Basic mode samples the complete transcript for source clues and is labeled `basic`; it is not a substitute for model understanding.
- AI mode chunks long transcripts, extracts sections with evidence per chunk, and reduces the candidates.
- Approved outlines can become knowledge nodes without sending the complete transcript again. Without a usable outline, keep the manual editing path.
- A reviewed source may produce zero or more nodes. Zero is valid when there is no reusable knowledge.
- Source timestamps are retained for verification, but saved records have shown out-of-range model times. Duration and transcript-grounding validation, plus historical repair, remain pending; do not claim that a nonempty evidence field proves correctness.

## Jobs

Transcription and AI curation use `processing_jobs`. Only one worker runs locally. A process restart moves stale `running` jobs back to `queued`; the underlying course manifest provides file-level resume behavior. Re-importing a URL must not create another active job for the same source and job type.

## Search

SQLite FTS5 is the first-stage index. Literal `LIKE` fallback preserves useful Chinese matching when tokenization is weak. Search reads approved knowledge and source outlines; unreviewed material is excluded by default. The user may explicitly request AI synthesis of a cited answer from retrieved results; it does not replace retrieval or require a predefined question-answer set. Search must include timestamps and original URLs when available and say `没有直接答案` when no evidence is found.

## Topic tree

`knowledge_topics` and `topic_units` organize approved knowledge units without rewriting them. Topics may be nested to any depth, and one unit may belong to multiple topics. The topic tree is an organizational view: source links and review state remain attached to the underlying knowledge unit. Cross-topic relations such as prerequisite, contrast, and complement are intentionally deferred until the hierarchy has been validated with real use.

Before creating or changing a topic structure, the optional model may produce an editable organization proposal from an already reviewed video outline and that source's knowledge units. A proposal classifies the material as an action playbook, topic dossier, learning map, or reference index, then suggests a root topic, branches, and node placement. Showing a proposal never changes the database. Only the explicit confirmation action may create topics or move the knowledge units that belong to that source; unrelated nodes must not be moved.

## Model settings and privacy

The optional client accepts an OpenAI-compatible Chat Completions base URL, model name, and bearer API key. The key may come from `VIDEO_KB_LLM_API_KEY` or the current web process. Explicit opt-in may persist it only through the bundled Windows Credential Manager helper; a local settings file stores the service address and model names, not the key. Base URL and model may be shown in the UI; the key must not be returned, logged, placed in job payloads, or committed. Cloud enhancement sends relevant transcript or retrieval text to the configured service.

The web server binds to `127.0.0.1`, accepts same-host requests only, limits JSON request size, and serves only bundled web assets. The workflow does not scrape private collections, read browser cookies, or bypass login, CAPTCHA, payment, region, membership, or DRM controls.

## Migration and backups

`schema_meta.schema_version` controls database upgrades. Before upgrading an unversioned knowledge database, copy it to `知识库/backups/`. Migration must preserve source counts and file paths. It may repair legacy workflow semantics but must not move, rewrite, or re-transcribe source files.
