"""Signals are not semantic verdicts. Only deterministic failures block directly."""
import re
import unicodedata
from collections import Counter

from ..narrative.schemas import NarrativeError
from .coverage import read_coverage
from .versions import read_version

_DIGITS = {c: i for i, c in enumerate("零一二三四五六七八九")}
_DIGITS.update({"〇": 0, "两": 2})


def _chinese_number(text):
    if not any(c in text for c in "十百千万"):
        return int("".join(str(_DIGITS[c]) for c in text))
    total, current = 0, 0
    for c in text:
        if c in _DIGITS:
            current = _DIGITS[c]
        else:
            unit = {"十": 10, "百": 100, "千": 1000, "万": 10000}[c]
            if unit == 10000:
                total = (total+current)*unit; current = 0
            else:
                total += (current or 1)*unit; current = 0
    return total+current


def numbers(text):
    text = unicodedata.normalize("NFKC", text)
    # Chinese numerals are considered only beside explicit numeric units.
    values = re.findall(r"\d+(?:\.\d+)?|[零〇一二三四五六七八九十百千万两]+(?=分钟|分|秒|小时|時間|日|天|年|章|级|級|人|个|個|倍|点|點)", text)
    return Counter(str(_chinese_number(v)) if not v[0].isdigit() else str(float(v)) if "." in v else str(int(v)) for v in values)


def run_checks(view, version_id, coverage_id, *, profile='legacy'):
    if profile not in {'legacy','audit-v2','p6-mvp'}:
        raise NarrativeError('invalid_record','unknown checks profile')
    v, coverage = read_version(view, version_id), read_coverage(view, version_id, coverage_id)
    hard, signals = [], []
    if not v["text"].strip(): hard.append("empty_translation")
    if v["finish_reason"] != "stop": hard.append("incomplete_generation")
    if not coverage["mapping_complete"]: hard.append("incomplete_mapping")
    source_text = "\n".join(s["text"] for s in v["source_segments"])
    diagnostics = []
    for recovery in coverage.get('recoveries', []):
        source_ids, target_ids = set(recovery['source_paragraph_ids']), set(recovery['target_paragraph_ids'])
        signals.append({'id': recovery['id'], 'kind': 'mapping_gap',
            'reason': 'possible_omission' if recovery['missing_source_paragraph_ids'] else 'possible_addition_or_mapping_omission',
            'source_refs': [r['alias'] for r in v['source_segments'] if r['id'] in source_ids],
            'target_refs': [r['alias'] for r in v['segments'] if r['id'] in target_ids],
            'unmapped_source_refs': [r['alias'] for r in v['source_segments'] if r['id'] in recovery['missing_source_paragraph_ids']],
            'unmapped_target_refs': [r['alias'] for r in v['segments'] if r['id'] in recovery['missing_target_paragraph_ids']],
            'source_text': [r['text'] for r in v['source_segments'] if r['id'] in source_ids],
            'target_text': [r['text'] for r in v['segments'] if r['id'] in target_ids],
            'instruction': '程序只恢复粗粒度结构覆盖。逐段检查原未映射项是否漏译、增译或仅为映射遗漏；不得因并组而直接驳回。'})
    if profile == 'p6-mvp':
        from ..mvp.numbers import aligned_number_signals
        quantities_signals, diagnostics = aligned_number_signals(v, coverage)
        signals.extend(quantities_signals)
    elif numbers(source_text) != numbers(v["text"]):
        signals.append({"id": "numbers", "kind": "numbers_or_units", "source": dict(numbers(source_text)), "target": dict(numbers(v["text"]))})
    ratio = len(v["text"])/max(1, len(source_text))
    if not .35 <= ratio <= 2:
        signals.append({"id": "length", "kind": "length", "ratio": round(ratio, 3)})
    if re.search(r"[ぁ-ゖァ-ヺ]", v["text"]): signals.append({"id": "kana", "kind": "japanese_residue"})
    for left, right in [("「", "」"), ("『", "』"), ("（", "）"), ("《", "》")]:
        if v["text"].count(left) != v["text"].count(right):
            signals.append({"id": "brackets:"+left, "kind": "unbalanced_brackets"})
    if "@@" in v["text"] or "```" in v["text"]:
        signals.append({"id": "format", "kind": "machine_markup"})
    if profile in {'audit-v2','p6-mvp'}:
        from .signals import residue_signals
        signals.extend(residue_signals(v))
    data = {"schema": "translation-checks-v1", "version_id": version_id,
        "coverage_id": coverage_id, "hard_failures": hard, "signals": signals,
        "semantic_coverage": "not_proven_by_rules", "state": "checked"}
    if profile != 'legacy': data['profile'] = profile
    if profile == 'p6-mvp': data['diagnostics'] = diagnostics
    return view.store.put('translation-checks',data)


def read_checks(view, version_id, checks_id):
    c = view.store.get(checks_id, "translation-checks")
    if c["version_id"] != version_id or run_checks(view, version_id, c["coverage_id"],profile=c.get('profile','legacy')) != checks_id:
        raise NarrativeError("conflict", "check report is stale or belongs to another version")
    return c
