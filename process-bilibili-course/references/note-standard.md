# Note standard

## Detailed per-episode notes

Treat each detailed note as a faithful, readable reconstruction of the complete lesson, not as a summary.

Required behavior:

- Preserve all distinct information points.
- Preserve each example as an event: who or what was involved, what happened, why it mattered, and what conclusion the lecturer drew.
- Preserve qualifications, contrasts, numbers, sequences, and causal reasoning.
- Remove filler words, false starts, and repeated oral phrasing only when no information is lost.
- Reorder adjacent sentences only to repair transcription flow; do not change the lecturer's logic.
- Do not add facts, modern updates, criticism, or outside interpretation.
- Correct obvious ASR errors only with high confidence. Otherwise use cautious wording or `[专有名词待核]`.
- Use descriptive `##` headings. Avoid a generic template that forces unrelated content into fixed sections.
- Keep the title linked to the timestamped transcript.

Example heading:

```markdown
# [第 31 集：从销售产品到销售解决方案（详细整理）](<../../../后台处理文件/课程名/时间戳逐字稿/31-transcript-timestamped.md>)
```

### Conditional navigation

Read the manifest before adding navigation; count active unique episodes after skipped compilations are excluded.

- If the count is greater than one, add “回到目录” navigation at both the top and bottom of every detailed note. Add “下一篇” only when a later active episode exists.
- If the count is one, do not add “回到目录” or “下一篇” navigation. Remove generated navigation if the manifest changed from a series to a single retained video.
- Run `scripts/course_utils.py index` to apply this rule deterministically and idempotently.

### Coverage QA

Before marking an episode complete, compare the note with the full transcript and ask:

1. Is every named example present?
2. Does each example retain the actual event rather than only a company or concept name?
3. Are the lecturer's explanation and conclusion preserved?
4. Are any unique numbers, lists, models, or steps missing?
5. Did the note introduce claims absent from the transcript?
6. Is the later batch as detailed as the earlier batch?

Do not use a fixed short word limit. A long lesson with many examples requires a long note.

## Concise course collection

Create the concise collection only after detailed notes are complete. It serves review and may compress aggressively.

For each episode retain:

- the central idea;
- essential model or steps;
- one or more indispensable examples;
- the practical conclusion;
- a link to the raw transcript.

Do not use the concise collection as a substitute for the detailed notes.

## Language

Write notes in the user's language unless asked otherwise. For English lessons, preserve important English terminology alongside a clear Chinese explanation when the user is Chinese-speaking.
