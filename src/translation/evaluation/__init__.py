"""Visibility first: prepare generation-only inputs separately from sealed references."""
from __future__ import annotations

import random
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from ..schemas import VisibilityPolicy
from ..storage import atomic_text, digest, read_json, read_jsonl, write_json, write_jsonl

DEV_NUMBERS = [3, 83, 208, 333, 508, 663, 775, 883, 1003, 1128, 1233, 1298]
SMOKE_KEYS = ["main:3", "main:775", "main:1233"]


def create_splits(chapters: list[dict], output: Path) -> dict:
    ids = {c["match_key"]: c["chapter_id"] for c in chapters if c["language"] == "ja" and c["kind"] == "narrative"}
    rng = random.Random(20261015)
    available = [n for n in range(1, 1308) if n not in DEV_NUMBERS]
    selected = rng.sample(available, 48)
    result = {"version": "split-v1", "seed": 20261015, "development": [ids[f"main:{n}"] for n in DEV_NUMBERS],
              "validation": [ids[f"main:{n}"] for n in sorted(selected[:24])],
              "blind_test": [ids[f"main:{n}"] for n in sorted(selected[24:])],
              "smoke_keys": SMOKE_KEYS, "limitations": "Chapter-level retained sets; future continuous windows must avoid these targets and development overlap."}
    result["fingerprint"] = digest(result)
    path = output / "splits.json"
    if path.exists() and read_json(path) != result:
        raise ValueError("split file already frozen with different content")
    write_json(path, result)
    return result


def dependency_allowed(source_dependencies: set[str], policy: VisibilityPolicy, source_orders: dict[str, int]) -> bool:
    if source_dependencies & set(policy.hidden_source_ids + policy.reserved_source_ids):
        return False
    if policy.mode == "sequential" and any(source_orders.get(cid, 10**12) > policy.cutoff_order for cid in source_dependencies):
        return False
    return True


def visible_examples(catalog: list[dict], policy: VisibilityPolicy, source_orders: dict[str, int], target: dict, limit=2, max_distance=8) -> list[dict]:
    allowed = []
    for item in catalog:
        if item.get("target_keys") and target.get("match_key") not in item["target_keys"]:
            continue
        dependencies = set(item["source_dependencies"])
        if item.get("status") != "reviewed_prefix" or not dependency_allowed(dependencies, policy, source_orders):
            continue
        if item["source_chapter_id"] not in source_orders:
            continue
        if abs(source_orders[item["source_chapter_id"]] - target["order"]) > max_distance:
            continue
        # The complete transitive dependency list is part of the catalog contract.
        allowed.append(item)
    allowed.sort(key=lambda item: (abs(source_orders[item["source_chapter_id"]] - target["order"]), source_orders[item["source_chapter_id"]]))
    return allowed[:limit]


def prepare(chapters: list[dict], groups: list[dict], catalog: list[dict], splits: dict, keys: list[str], output: Path,
            *, mode="single_gap", cutoff_order=None, context_builder=None) -> dict:
    lookup = {c["chapter_id"]: c for c in chapters}
    source = {c["match_key"]: c for c in chapters if c["language"] == "ja" and c["kind"] == "narrative"}
    orders = {c["chapter_id"]: c["order"] for c in source.values()}
    selected = [source[key] for key in keys]
    hidden = [c["chapter_id"] for c in selected]
    reserved = splits["validation"] + splits["blind_test"]
    policy = VisibilityPolicy(mode, hidden, reserved, cutoff_order)
    for example in catalog:
        cid = example["source_chapter_id"]
        source_chapter = lookup.get(cid)
        if source_chapter is None or cid not in example["source_dependencies"] or example["source_order"] != source_chapter["order"]:
            raise ValueError("invalid example provenance/order")
        if example.get("source_revision_id") and example["source_revision_id"] != source_chapter["revision_id"]:
            raise ValueError("stale example source revision")
        if example.get("translation_chapter_id"):
            target_chapter = lookup.get(example["translation_chapter_id"])
            if not target_chapter or target_chapter["language"] != "zh" or example["translation_revision_id"] != target_chapter["revision_id"]:
                raise ValueError("stale example translation revision")
            if source_chapter.get("match_key") != target_chapter.get("match_key"):
                raise ValueError("example source/translation chapter identity mismatch")
            if example.get("review", {}).get("purposes") is not None and example.get("status") == "reviewed_prefix":
                if example["review"].get("decision") != "accepted" or not example["review"]["purposes"].get("few_shot"):
                    raise ValueError("example review does not authorize few-shot")
            if target_chapter.get("metadata", {}).get("reference_integrity", {}).get("order_status") == "quarantined":
                raise ValueError("quarantined translation cannot supply examples")
            if target_chapter.get('metadata', {}).get('reference_integrity', {}).get('parallel_body_eligible') is False:
                raise ValueError('Reference integrity hold prevents examples')
    by_source = {s["chapter_id"]: g for g in groups if g["status"] == "confirmed" and g["relation"] == "1:1" for s in g["source_spans"]}
    tasks, references = [], []
    for chapter in selected:
        if mode == "sequential" and chapter["order"] <= cutoff_order:
            raise ValueError("sequential target must follow cutoff")
        group = by_source.get(chapter["chapter_id"])
        if not group:
            raise ValueError("selected chapter requires confirmed correspondence")
        translation = lookup[group["translation_spans"][0]["chapter_id"]]
        if group["source_spans"][0].get("revision_id") != chapter["revision_id"] or group["translation_spans"][0].get("revision_id") != translation["revision_id"]:
            raise ValueError("stale chapter correspondence")
        if translation.get("match_key") != chapter.get("match_key"):
            raise ValueError("reference chapter identity mismatch")
        if translation.get("metadata", {}).get("reference_integrity", {}).get("order_status") == "quarantined":
            raise ValueError("quarantined translation cannot supply evaluation references")
        if translation.get('metadata', {}).get('reference_integrity', {}).get('evaluation_eligible') is False:
            raise ValueError('Reference integrity hold prevents evaluation references')
        examples = visible_examples(catalog, policy, orders, chapter)
        task = {"source": {k: chapter[k] for k in ["chapter_id", "revision_id", "order", "title", "text", "match_key"]},
                "examples": examples, "visibility": asdict(policy)}
        if not examples:
            raise ValueError("B baseline requires at least one reviewed visible example")
        if context_builder is not None:
            task["context_bundle"] = context_builder(task).public()
        task["task_id"] = digest(task)
        tasks.append(task)
        references.append({"task_id": task["task_id"], "source_id": chapter["chapter_id"],
                           "translation_id": translation["chapter_id"], "text": translation["text"],
                           "quality": group.get("review", {}).get("reference_quality", "unreviewed"),
                           "review": group.get("review", {})})
    preparation = {"version": "prepared-v1", "policy": asdict(policy), "split_fingerprint": splits["fingerprint"],
                   "task_ids": [t["task_id"] for t in tasks], "input_fingerprint": digest(tasks)}
    existing_manifest = output / "generation/manifest.json"
    if existing_manifest.exists() and read_json(existing_manifest) != preparation:
        raise ValueError("prepared experiment is frozen; use a new experiment name")
    write_jsonl(output / "generation" / "tasks.jsonl", tasks)
    write_json(output / "generation" / "manifest.json", preparation)
    write_jsonl(output / "sealed" / "references.jsonl", references)
    return preparation


def char_f2(hypothesis: str, reference: str, max_order=6) -> float:
    """Diagnostic character n-gram F2, not a semantic or literary quality metric."""
    hypothesis = "".join(c for c in hypothesis if not c.isspace() and c not in "\u200b\u200c\u200d\ufeff")
    reference = "".join(c for c in reference if not c.isspace() and c not in "\u200b\u200c\u200d\ufeff")
    scores = []
    for order in range(1, max_order + 1):
        h = Counter(hypothesis[i:i+order] for i in range(len(hypothesis)-order+1))
        r = Counter(reference[i:i+order] for i in range(len(reference)-order+1))
        if not h or not r:
            continue
        overlap = sum((h & r).values())
        precision, recall = overlap / sum(h.values()), overlap / sum(r.values())
        scores.append(5 * precision * recall / (4 * precision + recall) if precision + recall else 0)
    return round(100 * sum(scores) / len(scores), 3) if scores else 0


def reports(prepared: Path, run: Path):
    import html
    tasks = read_jsonl(prepared / "generation/tasks.jsonl")
    references = {r["task_id"]: r for r in read_jsonl(prepared / "sealed/references.jsonl")}
    rows, sections, blind = [], [], []
    rng = random.Random(20261015)
    for task in tasks:
        reference = references[task["task_id"]]
        outputs = []
        for variant in ["A", "B", "C"]:
            path = run / "translations" / f"{task['task_id']}-{variant}.json"
            if not path.exists():
                continue
            result = read_json(path)
            metric = char_f2(result["text"], reference["text"])
            rows.append({"key": task["source"]["match_key"], "variant": variant, "char_f2_diagnostic": metric,
                         "reference_quality": reference["quality"], "output_characters": len(result["text"]),
                         "reference_characters": len(reference["text"]), "usage": result["usage"],
                         "elapsed_seconds": result["elapsed_seconds"], "checks": result["checks"],
                         "knowledge_context_fingerprint": task.get("context_bundle", {}).get("fingerprint") if variant == "C" else None,
                         "knowledge_revision": task.get("context_bundle", {}).get("knowledge_revision") if variant == "C" else None,
                         "context_token_accounting": task.get("context_bundle", {}).get("token_accounting") if variant == "C" else None,
                         "omitted_candidates": task.get("context_bundle", {}).get("omitted_candidates", []) if variant == "C" else [],
                         "unresolved_requirements": task.get("context_bundle", {}).get("unresolved_requirements", []) if variant == "C" else [],
                         "price": "unknown"})
            outputs.append((variant, result["text"]))
        esc = html.escape
        sections.append(f"<h2>{esc(task['source']['match_key'])} {esc(task['source']['title'])}</h2><p>Reference quality: {esc(reference['quality'])}</p><div class='columns'><section><h3>原文</h3><pre>{esc(task['source']['text'])}</pre></section><section><h3>参考译文</h3><pre>{esc(reference['text'])}</pre></section>" + "".join(f"<section><h3>{v}</h3><pre>{esc(t)}</pre></section>" for v,t in outputs) + "</div>")
        rng.shuffle(outputs)
        blind.append({"task_id": task["task_id"], "key": task["source"]["match_key"], "candidates": [{"label": f"候选{i+1}", "variant": v} for i,(v,t) in enumerate(outputs)]})
        # Blind pages have source and anonymous outputs, no references or model/variant names.
        blind_text = f"<h2>{esc(task['source']['title'])}</h2><div class='columns'><section><h3>原文</h3><pre>{esc(task['source']['text'])}</pre></section>" + "".join(f"<section><h3>候选{i+1}</h3><pre>{esc(t)}</pre></section>" for i,(v,t) in enumerate(outputs)) + "</div>"
        atomic_text(run / "reports" / "blind" / f"{task['task_id']}.html", _page(blind_text))
    write_json(run / "reports/metrics.json", {"rows": rows, "warning": "Character overlap is diagnostic only. References marked suspect are not trusted gold. No automatic literary-quality conclusion."})
    write_json(run / "reports/sealed_blind_key.json", blind)
    atomic_text(run / "reports/comparison.html", _page("".join(sections)))
    lines = ["# Initial translation results", "", "Character F2 is a descriptive overlap metric; it is not a quality verdict, especially for suspect references.", "",
             "| Chapter | Variant | char-F2 | Reference quality | Output chars | Prompt tokens | Output tokens | Seconds |", "| --- | --- | ---: | --- | ---: | ---: | ---: | ---: |"]
    for r in rows:
        lines.append(f"| {r['key']} | {r['variant']} | {r['char_f2_diagnostic']} | {r['reference_quality']} | {r['output_characters']} | {r['usage'].get('prompt_tokens','unknown')} | {r['usage'].get('completion_tokens','unknown')} | {r['elapsed_seconds']} |")
    atomic_text(run / "reports/results.md", "\n".join(lines) + "\n")
    annotation_path = run / "reports/error_annotations.jsonl"
    previous = {(a["key"],a["variant"]):a for a in read_jsonl(annotation_path)} if annotation_path.exists() else {}
    write_jsonl(annotation_path, [previous.get((r["key"],r["variant"]),{"key": r["key"], "variant": r["variant"], "review_status": "pending", "errors": []}) for r in rows])
    return rows


def _page(content):
    return "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>翻译审查</title><style>body{font-family:system-ui;margin:24px}.columns{display:flex;gap:20px;align-items:flex-start}section{flex:1;min-width:0}pre{white-space:pre-wrap;line-height:1.6;font-family:inherit}h2{border-top:1px solid #ccc;padding-top:20px}</style><body>" + content + "</body></html>"
