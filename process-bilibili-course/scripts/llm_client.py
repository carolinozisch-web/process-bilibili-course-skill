#!/usr/bin/env python3
"""Optional OpenAI-compatible enrichment without persisting credentials."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class LLMSettings:
    base_url: str = ""
    model: str = ""
    api_key: str = ""

    @classmethod
    def from_environment(cls) -> "LLMSettings":
        return cls(
            base_url=os.environ.get("VIDEO_KB_LLM_BASE_URL", ""),
            model=os.environ.get("VIDEO_KB_LLM_MODEL", ""),
            api_key=os.environ.get("VIDEO_KB_LLM_API_KEY", ""),
        )

    @property
    def configured(self) -> bool:
        return bool(self.base_url.strip() and self.model.strip() and self.api_key.strip())

    def public(self) -> dict:
        return {
            "base_url": self.base_url,
            "model": self.model,
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

    @property
    def endpoint(self) -> str:
        base = self.settings.base_url.rstrip("/")
        return base if base.endswith("/chat/completions") else f"{base}/chat/completions"

    def complete_json(self, system: str, prompt: str, timeout: float = 120) -> dict:
        headers = {"Authorization": f"Bearer {self.settings.api_key}", "Content-Type": "application/json"}
        payload = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        }
        try:
            with httpx.Client(transport=self.transport, timeout=timeout) as client:
                response = client.post(self.endpoint, headers=headers, json=payload)
                response.raise_for_status()
                body = response.json()
            content = body["choices"][0]["message"]["content"]
            return parse_json_object(content)
        except (httpx.HTTPError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise LLMError(f"模型服务返回异常：{exc}") from exc

    def test(self) -> dict:
        result = self.complete_json(
            "Return JSON only.",
            'Return exactly {"ok": true}.',
            timeout=30,
        )
        return {"ok": bool(result.get("ok")), "model": self.settings.model}


def parse_json_object(content: str) -> dict:
    text = content.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]
    value = json.loads(text)
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


def triage_with_ai(client: OpenAICompatibleClient, transcript: str, title: str = "") -> dict:
    chunks = chunk_text(transcript)
    candidates: list[dict] = []
    system = "你负责从视频逐字稿提取可验证信息。只输出合法 JSON，不补充原文没有的事实。"
    for index, chunk in enumerate(chunks, 1):
        result = client.complete_json(system, f"""
视频标题：{title}
这是逐字稿第 {index}/{len(chunks)} 段。请输出：
{{"points":[{{"text":"不超过18字的具体观点或方法","time":"原文时间，如00:42；没有则空"}}],
  "keywords":["关键词"],"topics":["主题"]}}
最多提取 3 个互不重复、能由原文支持的要点。

逐字稿：
{chunk}
""")
        candidates.extend(result.get("points", []))
    if len(chunks) > 1:
        reduced = client.complete_json(system, f"""
从以下候选中选出最能代表整段视频且互不重复的 3 点。只输出
{{"points":[{{"text":"不超过18字","time":"证据时间"}}],"keywords":[],"topics":[]}}。
候选：{json.dumps(candidates, ensure_ascii=False)}
""")
        candidates = reduced.get("points", candidates)
        keywords = reduced.get("keywords", [])
        topics = reduced.get("topics", [])
    else:
        keywords = result.get("keywords", []) if chunks else []
        topics = result.get("topics", []) if chunks else []
    summary, points = compact_summary(candidates)
    evidence = []
    for index, candidate in enumerate(candidates[:3]):
        if isinstance(candidate, dict):
            evidence.append({"point": points[index], "time": str(candidate.get("time", ""))})
    return {
        "summary_50": summary,
        "point_1": points[0], "point_2": points[1], "point_3": points[2],
        "keywords": list(dict.fromkeys(keywords))[:12],
        "topic_candidates": list(dict.fromkeys(topics))[:6],
        "source_signals": ["full_transcript", "ai"],
        "possible_duplicates": [], "new_points": [],
        "usable_content_start": evidence[0].get("time") if evidence else None,
        "evidence": evidence, "ai_mode": "ai",
    }


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
            content = f"{result.get('title', '')}；{result.get('summary_50', '')}"
        evidence.append(f"[{index}] {content}")
    response = client.complete_json(
        "根据给定知识库证据回答。证据不足时明确说没有直接答案。只输出合法 JSON。",
        f'问题：{query}\n证据：\n' + "\n".join(evidence) + '\n输出 {"answer":"带[序号]引用的简洁回答"}',
        timeout=60,
    )
    return str(response.get("answer", "没有直接答案"))
