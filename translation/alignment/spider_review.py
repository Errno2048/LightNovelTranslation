"""Evidence-constrained LLM review of a small Spider alignment candidate set."""
from __future__ import annotations

import json
import re

from ..storage import digest

RELATIONS = {"equivalent", "partial", "rewritten", "unrelated", "unresolved"}

SYSTEM_PROMPT = """你负责对日文 Web 版与中文文库版的候选段落窗口做证据约束的内容核验。只比较随请求提供的原文，不使用作品记忆补证据。

候选关系的含义：
- equivalent：所给两个窗口表达了基本相同的命题/场景范围；这只适用于所给窗口，不代表整章相同。
- partial：存在明确重合内容，但有一侧还包含未覆盖内容，或只对应其中一部分。
- rewritten：可辨认为同一事件/场景，但主体、动作、因果、次序或事实发生实质改变。
- unrelated：没有足够的具体共同内容，主题/类型相似不算对应。
- unresolved：证据不足，不能判断。

标题与卷序只用于定位，不作为内容对应证据。对话相似、重复出现的背景设定、技能清单相似都不足以证明是同一段。双线叙事可以重排；不要强加顺序。逐项核对主体、动作、结果与局部上下文。若来源和参考只有主题相同，判 unrelated。若只在末段有重合，引用那部分并判 partial。

只返回一个 JSON 对象，不要 Markdown：
{"reviews":[{"candidate_id":"...","relation":"equivalent|partial|rewritten|unrelated|unresolved","source_quote":"逐字复制的日文短引文","reference_quote":"逐字复制的中文短引文","shared_content":"被两侧片段直接支持的共同内容；无则空串","differences":["具体可定位差异"],"confidence":"low|medium|high"}]}

每条候选必须恰好返回一次。若判 unrelated 或 unresolved，引文可为空。引文必须是输入文本的连续原样子串，不得翻译、改写或补字。不要建议允许用于 few-shot；关系结论仍需人工复核。"""


def replay_review(saved: dict, raw: dict, windows: dict[str, dict]) -> dict:
    """Reparse frozen inputs without reselecting candidates from newer rankings."""
    messages = raw["messages"]
    if (digest(messages) != saved["prompt_fingerprint"]
            or raw["raw_response"] != saved["raw_response"]):
        raise ValueError("saved review prompt/response fingerprint mismatch")
    if len(messages) != 2 or messages[0] != {"role": "system", "content": SYSTEM_PROMPT} or messages[1]["role"] != "user":
        raise ValueError("review instructions changed; use a new output directory")
    candidates = saved["candidate_inputs"]
    if digest([(c["candidate_id"], c["source_text_hash"], c["reference_text_hash"]) for c in candidates]) != saved["input_fingerprints"]:
        raise ValueError("saved candidate input fingerprint mismatch")
    fields = ("candidate_id", "source_title", "reference_volume", "reference_title", "similarity", "source_text", "reference_text")
    if json.loads(messages[1]["content"])["candidate_pairs"] != [{k: c[k] for k in fields} for c in candidates]:
        raise ValueError("saved candidate inputs differ from the original prompt")
    replay = []
    for candidate in candidates:
        current = dict(candidate)
        for side in ("source", "reference"):
            window = windows.get(candidate[f"{side}_window_id"])
            if window is None or window["text"] != candidate[f"{side}_text"] or digest(window["text"]) != candidate[f"{side}_text_hash"]:
                raise ValueError(f"saved {side} text/window revision changed")
            current[f"_{side}_window"] = window
        replay.append(current)
    return validate_review_response(saved["raw_response"], replay)


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
    try:
        value = json.loads(text)
    except ValueError:
        left, right = text.find("{"), text.rfind("}")
        if left < 0 or right <= left:
            raise ValueError("review response contains no JSON object") from None
        try:
            value = json.loads(text[left:right + 1])
        except ValueError:
            raise ValueError("review response contains invalid JSON") from None
    if not isinstance(value, dict) or not isinstance(value.get("reviews"), list):
        raise ValueError("review response schema mismatch")
    return value


def validate_review_response(text: str, candidates: list[dict]) -> dict:
    """Parse output and reject invented candidate IDs or unsupported quotes."""
    result = _extract_json(text)
    supplied = {c["candidate_id"]: c for c in candidates}
    reviews = result["reviews"]
    ids = [row.get("candidate_id") for row in reviews if isinstance(row, dict)]
    if len(ids) != len(reviews) or len(ids) != len(set(ids)) or set(ids) != set(supplied):
        raise ValueError("candidate coverage mismatch")
    for row in reviews:
        if row.get("relation") not in RELATIONS or row.get("confidence") not in {"low", "medium", "high"}:
            raise ValueError("invalid relation or confidence")
        if not isinstance(row.get("shared_content"), str) or not isinstance(row.get("differences"), list) or any(not isinstance(x, str) for x in row["differences"]):
            raise ValueError("invalid review detail fields")
        candidate = supplied[row["candidate_id"]]
        source_quote, reference_quote = row.get("source_quote", ""), row.get("reference_quote", "")
        if not isinstance(source_quote, str) or not isinstance(reference_quote, str):
            raise ValueError("quotes must be strings")
        if source_quote and source_quote not in candidate["source_text"]:
            raise ValueError("source quote is not an exact input substring")
        if reference_quote and reference_quote not in candidate["reference_text"]:
            raise ValueError("reference quote is not an exact input substring")
        if row["relation"] in {"equivalent", "partial", "rewritten"} and not (source_quote and reference_quote):
            raise ValueError("supported correspondence requires quotes from both sides")
        row["source_evidence_spans"] = _quote_spans(candidate, "source", source_quote) if source_quote else []
        row["reference_evidence_spans"] = _quote_spans(candidate, "reference", reference_quote) if reference_quote else []
        row["effective_relation"] = row["relation"]
        row["review_warnings"] = []
        if row["relation"] == "equivalent" and row["differences"]:
            row["effective_relation"] = "partial"
            row["review_warnings"].append("equivalent_downgraded_due_to_reported_differences")
        row["model_confidence_is_calibrated"] = False
    return {"reviews": reviews, "evidence_validated": True}


def _quote_spans(candidate: dict, side: str, quote: str) -> list[dict]:
    window = candidate.get(f"_{side}_window")
    if not isinstance(window, dict) or not isinstance(window.get("text_segments"), list):
        raise ValueError(f"{side} quote has no paragraph locator metadata")
    text = candidate[f"{side}_text"]
    start = text.find(quote)
    if start < 0:
        raise ValueError(f"{side} quote is not an exact input substring")
    if text.find(quote, start + 1) >= 0:
        raise ValueError(f"{side} quote occurs more than once; a unique quote is required")
    end = start + len(quote)
    covered = []
    previous_end = 0
    unmapped = list(quote)
    for segment in window["text_segments"]:
        if not (0 <= segment["text_start"] < segment["text_end"] <= len(text)
                and segment["text_start"] >= previous_end
                and 0 <= segment["paragraph_start"] < segment["paragraph_end"]
                and segment["text_end"] - segment["text_start"] == segment["paragraph_end"] - segment["paragraph_start"]):
            raise ValueError(f"{side} quote has invalid paragraph locator metadata")
        previous_end = segment["text_end"]
        left = max(start, segment["text_start"])
        right = min(end, segment["text_end"])
        if left < right:
            unmapped[left - start:right - start] = " " * (right - left)
            covered.append({
                "paragraph_id": segment["paragraph_id"],
                "start": segment["paragraph_start"] + left - segment["text_start"],
                "end": segment["paragraph_start"] + right - segment["text_start"],
            })
    if not covered or "".join(unmapped).strip():
        raise ValueError(f"{side} quote has no paragraph locator")
    return covered
