#!/usr/bin/env python3
"""Optional OpenAI-compatible enrichment without persisting credentials."""

from __future__ import annotations

import json
import os
import random
import re
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from transcript_evidence import EvidenceError, bind_outline, evidence_fields, transcript_segments


RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
# A job has its own delayed retry schedule. Keep the immediate retry small so a
# temporarily overloaded provider is not hit repeatedly by the same task.
MAX_TRANSIENT_RETRIES = 1
GEMINI_MIN_REQUEST_INTERVAL_SECONDS = 1.5
_gemini_request_lock = threading.Lock()
_gemini_next_request_at = 0.0


@dataclass
class LLMSettings:
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    fallback_model: str = ""

    @classmethod
    def from_environment(cls) -> "LLMSettings":
        return cls(
            base_url=os.environ.get("VIDEO_KB_LLM_BASE_URL", ""),
            model=os.environ.get("VIDEO_KB_LLM_MODEL", ""),
            api_key=os.environ.get("VIDEO_KB_LLM_API_KEY", ""),
            fallback_model=os.environ.get("VIDEO_KB_LLM_FALLBACK_MODEL", ""),
        )

    @property
    def configured(self) -> bool:
        return bool(self.base_url.strip() and self.model.strip() and (self.api_key.strip() or self.is_local_ollama))

    @property
    def is_local_ollama(self) -> bool:
        parsed = urlparse(self.base_url)
        return parsed.hostname in {"127.0.0.1", "localhost"} and parsed.port == 11434

    @property
    def is_gemini(self) -> bool:
        return urlparse(self.base_url).hostname == "generativelanguage.googleapis.com"

    def public(self) -> dict:
        return {
            "base_url": self.base_url,
            "model": self.model,
            "fallback_model": self.fallback_model,
            "configured": self.configured,
            "has_api_key": bool(self.api_key),
        }


class LLMError(RuntimeError):
    pass


class OpenAICompatibleClient:
    def __init__(self, settings: LLMSettings, transport: httpx.BaseTransport | None = None):
        if not settings.configured:
            raise ValueError("LLM settings are incomplete")
        self.settings = settings
        self.transport = transport
        self.last_model = settings.model

    @property
    def endpoint(self) -> str:
        base = self.settings.base_url.rstrip("/")
        return base if base.endswith("/chat/completions") else f"{base}/chat/completions"

    @property
    def is_local_ollama(self) -> bool:
        """Use Ollama's native API so Qwen can run without its slow thinking mode."""
        return self.settings.is_local_ollama

    @property
    def ollama_endpoint(self) -> str:
        parsed = urlparse(self.settings.base_url)
        return f"{parsed.scheme}://{parsed.netloc}/api/chat"

    def request_timeout(self, requested: float) -> float:
        # Free local models commonly run on CPU and need longer for a full transcript.
        return max(requested, 300) if self.is_local_ollama else requested

    def _candidate_models(self) -> list[str]:
        models = [self.settings.model]
        fallback = self.settings.fallback_model.strip()
        if fallback and fallback != self.settings.model and not self.is_local_ollama:
            models.append(fallback)
        return models

    def _cloud_payload(self, model: str, messages: list[dict[str, str]]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": 0.2,
        }
        # Gemini 3 defaults to medium reasoning. This task is constrained
        # evidence extraction, so low is sufficient and reduces the load.
        if self.settings.is_gemini and model.startswith("gemini-3."):
            payload["reasoning_effort"] = "low"
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _post(self, client: httpx.Client, endpoint: str, headers: dict[str, str], payload: dict[str, Any]) -> httpx.Response:
        """Keep every Gemini request in this process on one measured lane."""
        if not self.settings.is_gemini:
            return client.post(endpoint, headers=headers, json=payload)
        global _gemini_next_request_at
        with _gemini_request_lock:
            delay = _gemini_next_request_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            response = client.post(endpoint, headers=headers, json=payload)
            _gemini_next_request_at = time.monotonic() + GEMINI_MIN_REQUEST_INTERVAL_SECONDS
            return response

    def complete_json(self, system: str, prompt: str, timeout: float = 120) -> dict:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        headers = {"Authorization": f"Bearer {self.settings.api_key}", "Content-Type": "application/json"}
        endpoint = self.endpoint
        payload = self._cloud_payload(self.settings.model, messages)
        if self.is_local_ollama:
            # The OpenAI-compatible route does not reliably forward think=False for Qwen3.
            endpoint = self.ollama_endpoint
            headers = {"Content-Type": "application/json"}
            payload = {
                "model": self.settings.model,
                "messages": messages,
                "stream": False,
                "think": False,
                "options": {"temperature": 0.2},
            }
        try:
            with httpx.Client(transport=self.transport, timeout=self.request_timeout(timeout)) as client:
                for model_index, model in enumerate(self._candidate_models()):
                    if not self.is_local_ollama:
                        payload = self._cloud_payload(model, messages)
                    for attempt in range(MAX_TRANSIENT_RETRIES + 1):
                        try:
                            response = self._post(client, endpoint, headers, payload)
                            response.raise_for_status()
                            body = response.json()
                            content = body["message"]["content"] if self.is_local_ollama else body["choices"][0]["message"]["content"]
                            self.last_model = model
                            return parse_json_object(content)
                        except httpx.HTTPStatusError as exc:
                            retryable = exc.response.status_code in RETRYABLE_STATUS_CODES
                            if not retryable:
                                raise
                            if attempt == MAX_TRANSIENT_RETRIES:
                                if model_index + 1 == len(self._candidate_models()):
                                    raise
                                break
                        except httpx.RequestError:
                            if attempt == MAX_TRANSIENT_RETRIES:
                                if model_index + 1 == len(self._candidate_models()):
                                    raise
                                break
                        # One short retry is enough here. Longer retry delays
                        # are handled by the persistent job queue.
                        time.sleep((2 ** attempt) + random.uniform(0, 0.25))
        except (httpx.HTTPError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise LLMError(f"模型服务返回异常：{exc}") from exc

    def test(self) -> dict:
        result = self.complete_json(
            "Return JSON only.",
            'Return exactly {"ok": true}.',
            timeout=30,
        )
        return {"ok": bool(result.get("ok")), "model": self.last_model}


def parse_json_object(content: str) -> dict:
    text = content.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1)
    else:
        start = text.find("{")
        if start >= 0:
            text = text[start:]
    # Small local models sometimes repeat a valid JSON reply. Decode the first
    # complete object instead of treating an otherwise usable response as failed.
    value, _ = json.JSONDecoder().raw_decode(text)
    if not isinstance(value, dict):
        raise json.JSONDecodeError("expected object", text, 0)
    return value


def chunk_text(text: str, max_chars: int = 10000) -> list[str]:
    clean = text.strip()
    if not clean:
        return []
    lines = clean.splitlines()
    chunks, current, size = [], [], 0
    for line in lines:
        addition = len(line) + 1
        if current and size + addition > max_chars:
            chunks.append("\n".join(current))
            current, size = [], 0
        if len(line) > max_chars:
            for start in range(0, len(line), max_chars):
                part = line[start:start + max_chars]
                if current:
                    chunks.append("\n".join(current))
                    current, size = [], 0
                chunks.append(part)
            continue
        current.append(line)
        size += addition
    if current:
        chunks.append("\n".join(current))
    return chunks


def _point_text(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("text", "")
    return re.sub(r"\s+", "", str(value)).strip("；;，,。 ")


def compact_summary(points: list[Any]) -> tuple[str, list[str]]:
    clean = [_point_text(point) for point in points if _point_text(point)]
    defaults = ["未提取到独立方法", "缺少可验证步骤", "建议人工查看原文"]
    while len(clean) < 3:
        clean.append(defaults[len(clean)])
    clean = clean[:3]
    budgets = [13, 13, 13]
    clipped = [value[:budget] for value, budget in zip(clean, budgets)]
    summary = f"①{clipped[0]}；②{clipped[1]}；③{clipped[2]}"
    return summary[:50], clean


def triage_with_ai(client: OpenAICompatibleClient, transcript: str, title: str = "",
                   *, duration: object = None) -> dict:
    try:
        segments = transcript_segments(transcript, duration)
    except EvidenceError as exc:
        raise LLMError(str(exc)) from exc
    if not segments:
        raise LLMError("缺少带时间逐字稿，无法生成可定位的原文证据")
    numbered = "\n".join(f"[{row['segment_id']}] {row['excerpt']}" for row in segments)
    chunks = chunk_text(numbered)
    candidates: list[dict] = []
    system = "你负责还原视频的论述结构。只输出合法 JSON；每个判断只能使用逐字稿明确表达的信息。"
    for index, chunk in enumerate(chunks, 1):
        result = client.complete_json(system, f"""
视频标题：{title}
这是逐字稿第 {index}/{len(chunks)} 段。提取它在整条视频中的一个或多个逻辑章节，而不是把内容改写成问答。输出 JSON，包含 overview 和 sections 数组；overview 用一句话概括本段在视频中的作用。每个 section 包含：
- title：不超过 20 字的章节标题
- summary：60 到 110 字的完整说明，先写结论，再说明它如何承接视频主题
- points：0 到 3 条支撑这一结论的短要点
- evidence：1 到 3 条原文证据，每条只需 segment_id，例如 S000001。只能选本段给出的编号；不要生成或改写时间戳，程序会从原逐字稿填入时间和原文。
只保留能组成完整观点的章节。忽略开场宣传、关注引导和结尾预告。不得写原文没有的概念或事实。

逐字稿：
{chunk}
""")
        allowed_ids = set(re.findall(r"\[(S\d{6})\]", chunk))
        try:
            bound = bind_outline(result, [row for row in segments if row['segment_id'] in allowed_ids])
        except EvidenceError as exc:
            raise LLMError(str(exc)) from exc
        candidates.extend(bound.get("sections", []))
    if len(chunks) > 1:
        reduced = client.complete_json(system, f"""
把以下分段候选合成为整条视频的逻辑提纲。输出 JSON：overview 是 80 到 140 字的总判断；sections 是按原视频顺序排列的 2 到 5 个章节，每个章节保留 title、summary、points、evidence。evidence 只能引用候选中已有的 segment_id，不要输出时间。整体要让读者不看原视频也能理解：视频的中心观点、展开顺序和最后结论。不得把章节写成问题，也不得补充候选证据中没有的事实。
候选章节：{json.dumps(candidates, ensure_ascii=False)}
""")
        allowed_ids = {row['segment_id'] for section in candidates for row in section['evidence']}
        try:
            outline_raw = bind_outline(reduced, [row for row in segments if row['segment_id'] in allowed_ids])
        except EvidenceError as exc:
            raise LLMError(str(exc)) from exc
        keywords = reduced.get("keywords", [])
        topics = reduced.get("topics", [])
    else:
        outline_raw = {
            "overview": result.get("overview", ""),
            "sections": candidates,
        }
        keywords = result.get("keywords", []) if chunks else []
        topics = result.get("topics", []) if chunks else []
    outline = _normalize_video_outline(outline_raw)
    if candidates and not outline.get("sections"):
        raise LLMError("模型没有提取到带原文依据的视频结构")
    if not outline.get("sections"):
        raise LLMError("逐字稿中没有足够内容生成可验证的视频结构")
    points = [section["title"] for section in outline["sections"]]
    while len(points) < 3:
        points.append("")
    evidence = [
        {"section_index": index, "point": section["title"], **row}
        for index, section in enumerate(outline["sections"], 1) for row in section["evidence"]
    ]
    return {
        "summary_50": outline["overview"][:50],
        "point_1": points[0], "point_2": points[1], "point_3": points[2],
        "key_questions": [], "video_outline": outline,
        "keywords": list(dict.fromkeys(keywords))[:12],
        "topic_candidates": list(dict.fromkeys(topics))[:6],
        "source_signals": ["full_transcript", "ai"],
        "possible_duplicates": [], "new_points": [],
        "usable_content_start": evidence[0].get("time") if evidence else None,
        "evidence": evidence, "ai_mode": "ai",
    }


def _normalize_video_outline(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    overview = re.sub(r"\s+", " ", str(value.get("overview", "")).strip())[:220]
    sections = []
    raw_sections = value.get("sections", [])
    for item in raw_sections[:5] if isinstance(raw_sections, list) else []:
        if not isinstance(item, dict):
            continue
        title = re.sub(r"\s+", " ", str(item.get("title", "")).strip())[:48]
        summary = re.sub(r"\s+", " ", str(item.get("summary", "")).strip())[:180]
        points = [re.sub(r"\s+", " ", str(point).strip())[:120]
                  for point in item.get("points", []) if str(point).strip()][:4]
        evidence = []
        for row in item.get("evidence", []) if isinstance(item.get("evidence"), list) else []:
            if not isinstance(row, dict):
                continue
            time = str(row.get("time", "")).strip()[:20]
            excerpt = re.sub(r"\s+", " ", str(row.get("excerpt", "")).strip())[:220]
            if time and excerpt:
                evidence.append({"time": time, "excerpt": excerpt, **evidence_fields(row)})
        template_text = " ".join([title, summary, *points] + [
            f"{row['time']} {row['excerpt']}" for row in evidence
        ])
        if any(marker in template_text for marker in (
            "章节标题", "完整说明", "原文证据", "不超过20字", "60到110字", "time 和 excerpt",
        )):
            continue
        if title and summary and evidence:
            sections.append({"title": title, "summary": summary, "points": points, "evidence": evidence[:3]})
    if not overview:
        overview = "；".join(section["summary"] for section in sections[:2])[:180]
    return {"overview": overview, "sections": sections} if overview and sections else {}


def _normalize_triage_questions(values: object) -> list[dict]:
    if not isinstance(values, list):
        return []
    questions = []
    for item in values[:3]:
        if not isinstance(item, dict):
            continue
        text = _point_text(item)
        question = re.sub(r"\s+", " ", str(item.get("question", "")).strip())[:80]
        answer = re.sub(r"\s+", " ", str(item.get("answer", text)).strip())[:360]
        status = str(item.get("answer_status", "")).strip()
        if status not in {"answered", "partial", "question_only"}:
            status = "answered" if answer else "question_only"
        status_note = re.sub(r"\s+", " ", str(item.get("status_note", "")).strip())[:160]
        evidence = []
        rows = item.get("evidence", [])
        if not isinstance(rows, list) and item.get("time"):
            rows = [{"time": item.get("time"), "excerpt": text}]
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            time = str(row.get("time", "")).strip()[:20]
            excerpt = re.sub(r"\s+", " ", str(row.get("excerpt", "")).strip())[:220]
            if time or excerpt:
                evidence.append({"time": time, "excerpt": excerpt})
        template_text = " ".join([question, answer, status_note] + [
            f"{row['time']} {row['excerpt']}" for row in evidence
        ])
        # Treat copied prompt examples as a model failure, never as source knowledge.
        if any(marker in template_text for marker in (
            "用户会问的具体问题", "原文时间，如", "证据时间", "支持答案或判断的原文",
            "60到160字", "仅提出问题时留空", "判断说明",
        )):
            continue
        if not question and text:
            question = "这条内容的关键做法是什么？"
        if question in {"这条内容的关键做法是什么？", "这条内容给出的关键做法是什么？"}:
            continue
        supported_evidence = [row for row in evidence if row["time"] and row["excerpt"]]
        if status == "answered" and (not answer or not evidence):
            status = "partial" if answer else "question_only"
            status_note = status_note or ("答案缺少可验证的原文依据" if answer else "原文只提出了问题")
        if status == "answered" and len(answer) < 24:
            status = "partial"
            status_note = "原文回答过短，不能作为完整答案"
        if status == "answered" and not supported_evidence:
            status = "partial"
            status_note = "答案缺少带时间点的完整原文依据"
        elif status == "partial" and not status_note:
            status_note = "原文只提供了部分方向，不能补全为完整答案"
        elif status == "question_only":
            answer = ""
            status_note = status_note or "原文提出了这个问题，但没有提供可验证答案"
        if question:
            questions.append({
                "question": question,
                "answer": answer,
                "evidence": evidence[:3],
                "answer_status": status,
                "status_note": status_note,
            })
    return questions


def curate_with_ai(client: OpenAICompatibleClient, transcript: str, title: str = "") -> list[dict]:
    chunks = chunk_text(transcript)
    extracted: list[dict] = []
    system = "你把逐字稿整理成可复用的方法卡。只输出合法 JSON，不编造步骤、限制或时间点。"
    for index, chunk in enumerate(chunks, 1):
        result = client.complete_json(system, f"""
视频标题：{title}
这是逐字稿第 {index}/{len(chunks)} 段。提取零到三个可执行方法，只输出：
{{"units":[{{"title":"方法名","method_type":"类型","when_to_use":"适用场景",
"steps":["步骤"],"constraints":["限制"],"common_questions":["常见问题"],
"common_symptoms":["适用症状"],"keywords":["关键词"],"topic_tags":["主题"],
"segment_start":0,"segment_end":0}}]}}
如果没有具体方法，units 返回空数组。

逐字稿：
{chunk}
""")
        extracted.extend(result.get("units", []))
    if len(extracted) > 3:
        merged = client.complete_json(system, f"""
合并以下重复方法，保留最多 5 张互不重复的方法卡。字段保持不变，只输出 {{"units":[]}}。
{json.dumps(extracted, ensure_ascii=False)}
""")
        extracted = merged.get("units", extracted)
    return [normalize_unit(unit) for unit in extracted[:5] if unit.get("title")]


def normalize_unit(unit: dict) -> dict:
    value = {
        "title": str(unit.get("title", "")).strip(),
        "method_type": str(unit.get("method_type", "")).strip(),
        "when_to_use": str(unit.get("when_to_use", "")).strip(),
        "steps": [str(item).strip() for item in unit.get("steps", []) if str(item).strip()],
        "constraints": [str(item).strip() for item in unit.get("constraints", []) if str(item).strip()],
        "common_questions": [str(item).strip() for item in unit.get("common_questions", []) if str(item).strip()],
        "common_symptoms": [str(item).strip() for item in unit.get("common_symptoms", []) if str(item).strip()],
        "keywords": [str(item).strip() for item in unit.get("keywords", []) if str(item).strip()],
        "topic_tags": [str(item).strip() for item in unit.get("topic_tags", []) if str(item).strip()],
        "status": "approved",
    }
    for key in ("segment_start", "segment_end"):
        raw = unit.get(key)
        try:
            value[key] = float(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            value[key] = None
    return value


ORGANIZATION_TYPES = {
    "action_playbook": "行动指南",
    "topic_dossier": "专题档案",
    "learning_map": "课程地图",
    "reference": "资料索引",
}


def _clean_path(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [re.sub(r"\s+", " ", str(part)).strip()[:80] for part in value
            if str(part).strip()][:3]


def normalize_organization_plan(value: object, units: list[dict]) -> dict:
    """Keep a proposal bounded to the reviewed source nodes, never to raw text."""
    value = value if isinstance(value, dict) else {}
    allowed = {int(unit["id"]) for unit in units if unit.get("id") is not None}
    architecture_type = str(value.get("architecture_type", "topic_dossier"))
    if architecture_type not in ORGANIZATION_TYPES:
        architecture_type = "topic_dossier"
    root_raw = value.get("root") if isinstance(value.get("root"), dict) else {}
    root = {
        "mode": "existing" if root_raw.get("mode") == "existing" else "new",
        "title": re.sub(r"\s+", " ", str(root_raw.get("title", "")).strip())[:80],
        "description": re.sub(r"\s+", " ", str(root_raw.get("description", "")).strip())[:240],
    }
    try:
        existing_id = int(root_raw.get("existing_topic_id"))
    except (TypeError, ValueError):
        existing_id = None
    if existing_id:
        root["existing_topic_id"] = existing_id
    branches = []
    seen_paths = set()
    for raw in value.get("branches", [])[:12] if isinstance(value.get("branches"), list) else []:
        if not isinstance(raw, dict):
            continue
        path = _clean_path(raw.get("path"))
        if not path or tuple(path) in seen_paths:
            continue
        seen_paths.add(tuple(path))
        branches.append({
            "path": path,
            "description": re.sub(r"\s+", " ", str(raw.get("description", "")).strip())[:240],
        })
    valid_paths = {tuple(branch["path"]) for branch in branches}
    assignments, assigned = [], set()
    for raw in value.get("assignments", []) if isinstance(value.get("assignments"), list) else []:
        if not isinstance(raw, dict):
            continue
        try:
            unit_id = int(raw.get("unit_id"))
        except (TypeError, ValueError):
            continue
        path = _clean_path(raw.get("path"))
        if unit_id in allowed and unit_id not in assigned and tuple(path) in valid_paths | {()}:
            assignments.append({"unit_id": unit_id, "path": path})
            assigned.add(unit_id)
    return {
        "architecture_type": architecture_type,
        "reason": re.sub(r"\s+", " ", str(value.get("reason", "")).strip())[:240],
        "root": root, "branches": branches, "assignments": assignments,
    }


def basic_organization_plan(source: dict, units: list[dict], existing_topics: list[dict]) -> dict:
    """A useful editable suggestion when no cloud model is configured or available."""
    title = str(source.get("title", "")).strip()
    outline = source.get("video_outline") if isinstance(source.get("video_outline"), dict) else {}
    sections = outline.get("sections", []) if isinstance(outline.get("sections"), list) else []
    action_terms = "求职 秋招 面试 简历 投递 准备 方法 步骤 教程"
    learning_terms = "课程 入门 教学 课 回归 编程 语言"
    combined = f"{title} {' '.join(str(unit.get('title', '')) for unit in units)}"
    architecture_type = "action_playbook" if any(term in combined for term in action_terms.split()) else "topic_dossier"
    if any(term in combined for term in learning_terms.split()):
        architecture_type = "learning_map"
    branches = []
    for section in sections[:5]:
        if isinstance(section, dict) and str(section.get("title", "")).strip():
            branches.append({"path": [str(section["title"]).strip()[:80]],
                             "description": str(section.get("summary", "")).strip()[:240]})
    if not branches:
        branches = [{"path": [unit.get("title", "知识点")[:80]], "description": ""}
                    for unit in units[:5]]
    assignments = []
    for index, unit in enumerate(units):
        path = branches[min(index, len(branches) - 1)]["path"] if branches else []
        assignments.append({"unit_id": unit["id"], "path": path})
    return normalize_organization_plan({
        "architecture_type": architecture_type,
        "reason": "按这条视频已有的完整结构生成；确认前不会改动知识库。",
        "root": {"mode": "new", "title": title[:80] or "未命名专题",
                 "description": str(outline.get("overview", "")).strip()[:240]},
        "branches": branches, "assignments": assignments,
    }, units)


def propose_organization(client: OpenAICompatibleClient | None, source: dict, units: list[dict],
                         existing_topics: list[dict]) -> tuple[dict, str]:
    """Return an editable knowledge-tree proposal. It is never written automatically."""
    fallback = basic_organization_plan(source, units, existing_topics)
    if not client:
        return fallback, "basic"
    outline = source.get("video_outline") if isinstance(source.get("video_outline"), dict) else {}
    topic_context = [{"id": row.get("id"), "path": row.get("path")} for row in existing_topics[:80]]
    prompt = f"""
基于一条已经审核的视频结构，为个人知识库提出一个“待确认的归档方案”。这不是摘要，也不是问答；要体现整体逻辑。

可选 architecture_type 只能是：action_playbook（行动指南）、topic_dossier（专题档案）、learning_map（课程地图）、reference（资料索引）。
例如求职策略适合行动指南，行业或品牌分析适合专题档案，系列课程适合课程地图。不要把不同类型硬套成步骤。

只输出 JSON：
{{
  "architecture_type":"topic_dossier",
  "reason":"不超过80字，说明为何采用这种结构",
  "root":{{"mode":"new 或 existing","existing_topic_id":已有主题 id 或 null,"title":"新主题名","description":"主题整体说明"}},
  "branches":[{{"path":["一级主题","可选二级主题"],"description":"这部分覆盖什么"}}],
  "assignments":[{{"unit_id":知识点 id,"path":["该知识点所属分支"]}}]
}}
可选择 existing 仅当“已有主题”确实能容纳这条视频；否则 new。每个给定知识点最多归入一个分支。不要新造知识点或事实，最多 6 个分支。

视频标题：{source.get('title', '')}
视频总览：{outline.get('overview', '')}
视频章节：{json.dumps(outline.get('sections', []), ensure_ascii=False)}
这条视频已生成的知识点：{json.dumps([{'id': u.get('id'), 'title': u.get('title'), 'content': u.get('steps', [])[:2]} for u in units], ensure_ascii=False)}
已有主题：{json.dumps(topic_context, ensure_ascii=False)}
"""
    try:
        raw = client.complete_json(
            "你是知识架构师。只根据已审核结构设计可编辑的归档建议；绝不自动执行归档。只输出合法 JSON。",
            prompt, timeout=90,
        )
        plan = normalize_organization_plan(raw, units)
        if not plan["root"]["title"] and plan["root"]["mode"] != "existing":
            return fallback, "basic"
        return plan, "ai"
    except LLMError:
        return fallback, "basic"


def expand_query(client: OpenAICompatibleClient, query: str) -> list[str]:
    result = client.complete_json(
        "只输出合法 JSON。",
        f'为知识库检索扩展以下问题，输出 {{"terms":["词"]}}，最多5个关键词：{query}',
        timeout=45,
    )
    return [str(term).strip() for term in result.get("terms", []) if str(term).strip()][:5]


def answer_with_ai(client: OpenAICompatibleClient, query: str, results: list[dict]) -> str:
    evidence = []
    for index, result in enumerate(results[:8], 1):
        if result.get("kind") == "knowledge_unit":
            content = f"{result.get('title', '')}；{result.get('when_to_use', '')}；{'；'.join(result.get('steps', []))}"
        else:
            outline = result.get("video_outline") or {}
            sections = outline.get("sections", []) if isinstance(outline, dict) else []
            structure = "；".join(
                f"{section.get('title', '')}：{section.get('summary', '')}"
                for section in sections if isinstance(section, dict)
            )
            content = f"{result.get('title', '')}；{outline.get('overview', result.get('summary_50', ''))}；{structure}"
        evidence.append(f"[{index}] {content}")
    response = client.complete_json(
        "根据给定知识库证据回答。证据不足时明确说没有直接答案。只输出合法 JSON。",
        f'问题：{query}\n证据：\n' + "\n".join(evidence) + '\n输出 {"answer":"带[序号]引用的简洁回答"}',
        timeout=60,
    )
    return str(response.get("answer", "没有直接答案"))
