# Output layout

Use this structure for every course:

```text
<workspace>/
├─ 课程资料/
│  └─ <course>/
│     ├─ 00-课程入口/
│     │  ├─ <course>-详细笔记目录.md
│     │  └─ <course>-精炼笔记合集.md
│     ├─ 01-详细笔记/
│     │  └─ NN-notes.md
│     ├─ 02-逐字稿/
│     │  └─ NN-transcript.txt
│     └─ 03-音频/
│        └─ NN-audio.mp3
├─ 后台处理文件/
│  └─ <course>/
│     ├─ course-manifest.json
│     ├─ 时间戳逐字稿/NN-transcript-timestamped.md
│     ├─ 分段数据/NN-segments.json
│     ├─ 元数据/NN-metadata.json
│     └─ 原始缓存/NN/        # temporary streams only; normally empty after success
└─ .models/faster-whisper/   # local model cache; location is configurable
```

## Naming rules

- Use two-digit episode IDs through 99: `01`, `02`, … `78`.
- Derive order from Bilibili `page`, not download order or folder order.
- Keep the Bilibili part title in metadata and the note heading.
- Sanitize only Windows-invalid filename characters: `< > : " / \\ | ? *`.
- Never mix two courses in one course directory.

## Link rules

- Note heading → `../02-逐字稿/NN-transcript.txt`
- Detailed index → `../01-详细笔记/NN-notes.md`
- Concise collection → `../02-逐字稿/NN-transcript.txt`
- Validate every relative link after moves or renames.

## Retention rules

Keep:

- compressed MP3 audio;
- raw and timestamped transcripts;
- detailed notes and concise collection;
- segments, metadata, manifest, and logs.

Delete only after successful MP3 conversion and verification:

- downloaded `.m4s` audio streams;
- downloaded full video streams;
- temporary split chunks.

Do not delete source media on a failed conversion.
