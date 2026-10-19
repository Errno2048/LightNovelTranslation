"""Rule anchors, local dynamic programming, and explicit review patches."""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from pathlib import Path

from ..storage import atomic_text, digest, read_json, read_jsonl, write_json, write_jsonl


def span(chapter: dict) -> dict:
    return {"chapter_id": chapter["chapter_id"], "revision_id": chapter["revision_id"],
            "paragraph_ids": chapter["paragraph_ids"]}


def rules(chapters: list[dict], output: Path) -> dict:
    sides = {lang: [c for c in chapters if c["language"] == lang and c["kind"] == "narrative"] for lang in ("ja", "zh")}
    indices = {}
    for lang in sides:
        index = defaultdict(list)
        for chapter in sides[lang]:
            if chapter["match_key"]:
                index[chapter["match_key"]].append(chapter)
        indices[lang] = index
    groups, used = [], set()
    for ja in sides["ja"]:
        key = ja["match_key"]
        targets = indices["zh"].get(key, [])
        ambiguous = len(indices["ja"].get(key, [])) > 1 or len(targets) > 1 or key is None
        unique = len(targets) == 1 and not ambiguous
        zh = targets[0] if unique else None
        ratio = round(len(zh["text"]) / max(1, len(ja["text"])), 3) if zh else None
        group = {"group_id": "align:" + digest([ja["chapter_id"], zh["chapter_id"] if zh else None])[:20],
                 "source_spans": [span(ja)], "translation_spans": [span(zh)] if zh else [],
                 "relation": "1:1" if zh else "source_only", "status": "uncertain" if ambiguous else "candidate", "method": "unique_sequence_key" if zh else "unmatched",
                 "evidence": {"match_key": key, "target_source_length_ratio": ratio, "body_checked": False},
                 "reason": "needs_body_review" if zh else "ambiguous_key" if ambiguous else "missing_candidate"}
        if zh:
            used.add(zh["chapter_id"])
            if zh.get('metadata', {}).get('reference_integrity', {}).get('parallel_body_eligible') is False:
                group.update({'status': 'uncertain', 'reason': 'reference_integrity_hold'})
            if not 0.3 <= ratio <= 1.5:
                group["reason"] = "length_anomaly"
        groups.append(group)
    for zh in sides["zh"]:
        if zh["chapter_id"] not in used:
            groups.append({"group_id": "align:" + digest(zh["chapter_id"])[:20], "source_spans": [],
                           "translation_spans": [span(zh)], "relation": "target_only", "status": "uncertain", "method": "unmatched",
                           "reason": "extra_or_ambiguous_target", "evidence": {"match_key": zh["match_key"]}})
    # Re-running the rule stage must not silently destroy completed human reviews.
    existing = output / "groups.jsonl"
    if existing.exists():
        old = read_jsonl(existing)
        reviewed = [g for g in old if g.get("review") or g["status"]=="confirmed"]
        if reviewed:
            current = {g["group_id"]:g for g in groups}
            for item in reviewed:
                candidate = current.get(item["group_id"])
                if not candidate or candidate["source_spans"]!=item["source_spans"] or candidate["translation_spans"]!=item["translation_spans"]:
                    raise ValueError("reviewed alignment structure/revisions changed; use a new alignment branch")
                candidate.update({k:item[k] for k in ["status","reason","review"] if k in item})
    validate(groups, chapters)
    save(groups, output)
    return {"groups": len(groups), "relations": dict(Counter(g["relation"] for g in groups)), "statuses": dict(Counter(g["status"] for g in groups))}


def validate(groups: list[dict], chapters: list[dict]) -> None:
    lookup = {c["chapter_id"]: c for c in chapters}
    assigned = set()
    ids = set()
    for group in groups:
        if group["group_id"] in ids:
            raise ValueError("duplicate alignment group id")
        ids.add(group["group_id"])
        left, right = group["source_spans"], group["translation_spans"]
        expected = "target_only" if not left else "source_only" if not right else f"{1 if len(left)==1 else 'n'}:{1 if len(right)==1 else 'n'}"
        if group["relation"] != expected or not left + right:
            raise ValueError("alignment relation/span mismatch")
        if group["status"] not in {"candidate", "confirmed", "rejected", "uncertain", "edition_mismatch"}:
            raise ValueError("invalid alignment status")
        for side, language in [(left, "ja"), (right, "zh")]:
            for ref in side:
                chapter = lookup.get(ref["chapter_id"])
                if not chapter or chapter["revision_id"] != ref["revision_id"] or chapter["language"] != language:
                    raise ValueError("alignment references unknown/stale/wrong-language chapter")
                if group['status'] == 'confirmed' and language == 'zh' and chapter.get('metadata', {}).get('reference_integrity', {}).get('parallel_body_eligible') is False:
                    raise ValueError('Reference integrity hold prevents confirmed correspondence')
                paragraphs = ref["paragraph_ids"]
                if len(paragraphs) != len(set(paragraphs)) or not set(paragraphs) <= set(chapter["paragraph_ids"]):
                    raise ValueError("invalid paragraph range")
                keys = {(chapter["chapter_id"], p) for p in paragraphs}
                if keys & assigned:
                    raise ValueError("overlapping alignment assignment")
                assigned.update(keys)


def save(groups: list[dict], output: Path):
    write_jsonl(output / "groups.jsonl", groups)
    write_json(output / "alignment_manifest.json", {"version": "rules-v1", "fingerprint": digest(groups),
               "count": len(groups), "relations": dict(Counter(g["relation"] for g in groups)), "statuses": dict(Counter(g["status"] for g in groups))})
    lines = ["# Chapter alignment review", "", "Rule matches are candidates, not proof of semantic equivalence.", "",
             "| Key | Relation | Status | Length ratio | Reason |", "| --- | --- | --- | ---: | --- |"]
    lines += [f"| {g['evidence'].get('match_key')} | {g['relation']} | {g['status']} | {g['evidence'].get('target_source_length_ratio', '')} | {g.get('reason', '')} |" for g in groups]
    atomic_text(output / "alignment_report.md", "\n".join(lines) + "\n")


def apply_review(folder: Path, patch: dict, chapters: list[dict]):
    """Replacements allow split/merge/range corrections; explicit old IDs are removed."""
    groups = read_jsonl(folder / "groups.jsonl")
    if patch.get("corpus_fingerprint") and patch["corpus_fingerprint"] != digest([(c["chapter_id"],c["revision_id"]) for c in chapters]):
        raise ValueError("review patch is for a different corpus revision")
    by_id = {g["group_id"]: g for g in groups}
    for update in patch.get("updates", []):
        if update["group_id"] not in by_id:
            raise ValueError("unknown group in patch")
        allowed = {"group_id", "status", "reason", "review"}
        if set(update) - allowed or not update.get("review"):
            raise ValueError("review evidence required; use replacements for span edits")
        by_id[update["group_id"]].update(update)
    removed = set(patch.get("remove_group_ids", []))
    if not removed <= set(by_id):
        raise ValueError("unknown replacement target")
    groups = [g for g in groups if g["group_id"] not in removed] + patch.get("replacement_groups", [])
    validate(groups, chapters)
    previous = digest(read_jsonl(folder / "groups.jsonl"))
    patch_id = digest(patch)
    write_json(folder / "reviews" / f"{patch_id}.json", {"parent_fingerprint": previous, "patch": patch})
    save(groups, folder)


def local_sequence_align(left: list, right: list, score, *, max_group=2, gap_penalty=-0.45) -> dict:
    """Monotonic local DP with skips and split/merge transitions. score receives blocks."""
    n, m = len(left), len(right)
    dp = [[-math.inf] * (m + 1) for _ in range(n + 1)]
    back = {}
    dp[0][0] = 0.0
    for i in range(n + 1):
        for j in range(m + 1):
            moves = []
            if i < n:
                moves.append((1, 0, gap_penalty))
            if j < m:
                moves.append((0, 1, gap_penalty))
            for a in range(1, min(max_group, n - i) + 1):
                for b in range(1, min(max_group, m - j) + 1):
                    moves.append((a, b, float(score(left[i:i+a], right[j:j+b])) - 0.08 * (a+b-2)))
            for a, b, value in moves:
                candidate = dp[i][j] + value
                if candidate > dp[i+a][j+b]:
                    dp[i+a][j+b] = candidate
                    back[i+a, j+b] = (i, j, value)
    steps, i, j = [], n, m
    while i or j:
        pi, pj, value = back[i, j]
        steps.append({"source_indices": list(range(pi, i)), "target_indices": list(range(pj, j)), "score": value})
        i, j = pi, pj
    return {"score": dp[n][m], "steps": list(reversed(steps)), "method": "local_dp_v1", "calibrated_probability": False}


def cosine(a, b):
    return sum(x*y for x, y in zip(a, b)) / max(1e-12, math.sqrt(sum(x*x for x in a) * sum(x*x for x in b)))


def cached_embeddings(embedder, texts: list[str], folder: Path) -> dict:
    key = digest({"configuration":embedder.config.public(),"preprocess":"exact-text-v1","text_hashes":[digest(t) for t in texts]})
    path = folder / f"{key}.json"
    if path.exists():
        cached=read_json(path)
        if cached["key"] != key or len(cached["vectors"]) != len(texts) or any(len(v)!=embedder.config.dimension or any(not math.isfinite(x) for x in v) for v in cached["vectors"]):
            raise ValueError("invalid embedding cache; inspect before retrying")
        return {**cached,"usage":{},"original_usage":cached.get("usage",{}),"cache_hit":True}
    response=embedder.embed(texts)
    record={**response,"key":key,"cache_hit":False}
    write_json(path,record)
    return record


def semantic_probe(chapters: list[dict], keys: list[str], embedder, output: Path) -> dict:
    selected = [c for c in chapters if c["match_key"] in keys and c["kind"] == "narrative"]
    selected.sort(key=lambda c: (c["language"], c["match_key"]))
    # Keep inputs below the model's per-item budget; compare content, not translated titles.
    texts = [c["text"][:650] + "\n" + c["text"][-350:] for c in selected]
    result = cached_embeddings(embedder,texts,output/"embedding_cache")
    vectors = {c["chapter_id"]: v for c, v in zip(selected, result["vectors"])}
    cache_key = digest({"config": embedder.config.public(), "preprocess": "head650-tail350-v1", "text_hashes": [digest(t) for t in texts]})
    write_json(output / "vectors" / f"{cache_key}.json", {"key": cache_key, "config": embedder.config.public(), "chapters": [{"chapter_id": c["chapter_id"], "revision_id": c["revision_id"]} for c in selected], "vectors": result["vectors"]})
    rows = []
    targets = [c for c in selected if c["language"] == "zh"]
    for source in (c for c in selected if c["language"] == "ja"):
        ranked = sorted([(cosine(vectors[source["chapter_id"]], vectors[t["chapter_id"]]), t) for t in targets], key=lambda x:x[0], reverse=True)
        rows.append({"source_key": source["match_key"], "expected_key_from_sequence": source["match_key"],
                     "top_key": ranked[0][1]["match_key"], "top_similarity": ranked[0][0],
                     "margin": ranked[0][0] - ranked[1][0] if len(ranked)>1 else None,
                     "ranked": [{"key": c["match_key"], "similarity": score} for score, c in ranked]})
    report = {"method": "small_sequence_anchored_cross_language_retrieval", "candidate_keys": keys,
              "source_count": len(rows), "correct_top1": sum(r["source_key"] == r["top_key"] for r in rows),
              "usage": result["usage"], "cache_hit":result["cache_hit"], "rows": rows,
              "limitation": "Sequence keys are provisional labels, not independent manual gold. No calibrated acceptance threshold."}
    write_json(output / "semantic_probe.json", report)
    return report


def create_example_catalog(chapters: list[dict], paragraphs_path: Path, specification: dict, output: Path) -> list[dict]:
    """Materialize only explicitly reviewed prefix ranges, bound to the corpus revision."""
    import json
    fingerprint = digest([(c["chapter_id"],c["revision_id"]) for c in chapters])
    if specification["corpus_fingerprint"] != fingerprint:
        raise ValueError("example review is stale")
    index = {(c["language"],c["match_key"]): c for c in chapters if c["kind"]=="narrative"}
    selected_ids = {index[lang,item["key"]]["chapter_id"] for item in specification["prefixes"] for lang in ["ja","zh"]}
    paragraphs = defaultdict(list)
    with paragraphs_path.open(encoding="utf-8") as stream:
        for line in stream:
            p = json.loads(line)
            if p["chapter_id"] in selected_ids and p["kind"]=="body" and p["text"].strip():
                paragraphs[p["chapter_id"]].append(p)
    catalog = []
    for item in specification["prefixes"]:
        source, target = index["ja",item["key"]], index["zh",item["key"]]
        if target.get("metadata", {}).get("reference_integrity", {}).get("order_status") == "quarantined":
            raise ValueError("quarantined translation cannot supply examples")
        if target.get('metadata', {}).get('reference_integrity', {}).get('parallel_body_eligible') is False:
            raise ValueError('Reference integrity hold prevents examples')
        left = paragraphs[source["chapter_id"]][:item["source_paragraph_count"]]
        right = paragraphs[target["chapter_id"]][:item["translation_paragraph_count"]]
        if not left or not right or not item.get("review"):
            raise ValueError("reviewed nonempty prefix required")
        entry = {"source_chapter_id":source["chapter_id"], "translation_chapter_id":target["chapter_id"],
                 "source_revision_id":source["revision_id"], "translation_revision_id":target["revision_id"],
                 "source_order":source["order"], "source_dependencies":[source["chapter_id"]], "status":"reviewed_prefix",
                 "source_paragraph_ids":[p["paragraph_id"] for p in left], "translation_paragraph_ids":[p["paragraph_id"] for p in right],
                 "source_text":"\n\n".join(p["text"] for p in left), "translation_text":"\n\n".join(p["text"] for p in right),
                 "review":item["review"], "review_fingerprint":digest(specification)}
        catalog.append(entry)
    write_jsonl(output / "example_catalog.jsonl",catalog)
    return catalog


def semantic_window(sources: list[dict], targets: list[dict], embedder, output: Path) -> dict:
    """Small explicit anchor-bounded window. Results require review, never automatic acceptance."""
    if len(sources) > 12 or len(targets) > 12 or not sources or not targets:
        raise ValueError("semantic window requires 1–12 chapters on each side")
    if any(c["language"] != "ja" for c in sources) or any(c["language"] != "zh" for c in targets):
        raise ValueError("wrong window languages")
    sources, targets = sorted(sources,key=lambda c:c["order"]), sorted(targets,key=lambda c:c["order"])
    blocks = []
    for side in [sources,targets]:
        for size in [1,2]:
            for start in range(len(side)-size+1):
                blocks.append(side[start:start+size])
    texts = []
    for block in blocks:
        text = "\n\n".join(c["text"] for c in block)
        texts.append(text[:650]+"\n"+text[-350:])
    embedded = cached_embeddings(embedder,texts,output/"embedding_cache")
    vectors = {tuple(c["chapter_id"] for c in b): v for b,v in zip(blocks,embedded["vectors"])}
    def score(left,right):
        similarity = cosine(vectors[tuple(c["chapter_id"] for c in left)],vectors[tuple(c["chapter_id"] for c in right)])
        ratio = sum(len(c["text"]) for c in right)/max(1,sum(len(c["text"]) for c in left))
        return 4*(similarity-.8)+.2-.15*abs(math.log(max(ratio,1e-6)/.78))
    path = local_sequence_align(sources,targets,score,gap_penalty=-.35)
    groups=[]
    for step in path["steps"]:
        left=[sources[i] for i in step["source_indices"]]
        right=[targets[i] for i in step["target_indices"]]
        relation="target_only" if not left else "source_only" if not right else f"{1 if len(left)==1 else 'n'}:{1 if len(right)==1 else 'n'}"
        groups.append({"group_id":"window:"+digest([[c["chapter_id"] for c in left],[c["chapter_id"] for c in right]])[:20],
            "source_spans":[span(c) for c in left],"translation_spans":[span(c) for c in right],"relation":relation,
            "status":"uncertain","method":"semantic_local_dp_v1","reason":"uncalibrated_window_candidate",
            "evidence":{"path_transition_score":step["score"],"calibrated_probability":False}})
    validate(groups,sources+targets)
    report={"path":path,"groups":groups,"usage":embedded["usage"],"cache_hit":embedded["cache_hit"],"provider":embedder.config.public(),
            "limitation":"Heuristic scores, local window only; inspect and apply explicit replacements after review."}
    write_json(output/"window_candidates.json",report)
    return report
