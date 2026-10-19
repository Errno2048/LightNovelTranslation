"""Two-level lazy summaries. Catalog/query do not implicitly call a model.

Closed nodes can be reused at a later cutoff after checking their exact bilingual
dependencies. Refinements add immutable metadata revisions, never rewrite a
parent overview or assert entity identity.
"""
from dataclasses import asdict, dataclass, field

from ..providers import ProviderError
from ..schemas import GenerationRequest
from ..storage import digest
from .bilingual import (BilingualSummaryConfig, summary_messages, validate_content,
                        validate_pairs)
from .schemas import NarrativeError
from .summary_protocol import protocol_messages
from .summary_admission import ADMISSION_VERSION, decode_summary, component_quality
from .summary_repair import repair_summary, fidelity, repair_messages

LEGACY_PROMPT_VERSION = "lazy-two-level-bilingual-v2-tail-task"
PROMPT_VERSION = "lazy-two-level-bilingual-v3-resilient-flat"


@dataclass(frozen=True)
class MultiLevelConfig:
    parent_chapters: int = 100
    child_chapters: int = 10

    def __post_init__(self):
        if (type(self.parent_chapters) is not int or type(self.child_chapters) is not int or
                self.child_chapters <= 0 or self.parent_chapters < self.child_chapters or
                self.parent_chapters % self.child_chapters):
            raise NarrativeError("invalid_record", "positive, divisible parent/child sizes required")


@dataclass
class MultiLevelContextSpec:
    n_chapter: int = 3
    n_child: int = 2
    requested_child_ids: list[str] = field(default_factory=list)
    context_limit: int = 1048576
    max_output_tokens: int = 8192
    safety_tokens: int = 2048

    def __post_init__(self):
        if (any(type(v) is not int or v < 0 for v in (self.n_chapter, self.n_child, self.safety_tokens)) or
                type(self.max_output_tokens) is not int or self.max_output_tokens <= 0 or self.input_budget <= 0 or
                not isinstance(self.requested_child_ids, list) or
                any(not isinstance(v, str) for v in self.requested_child_ids) or
                len(set(self.requested_child_ids)) != len(self.requested_child_ids)):
            raise NarrativeError("invalid_record", "invalid multilevel context specification")

    @property
    def input_budget(self):
        return self.context_limit - self.max_output_tokens - self.safety_tokens


def _catalog(view, config):
    # Number physical reading positions before filtering reserved chapters, so a
    # hole or a later cutoff never shifts every subsequent boundary.
    prior = [(i, c) for i, c in enumerate(view.sources, 1)
             if view.policy.cutoff_order is None or c["order"] <= view.policy.cutoff_order]
    visible = {c["chapter_id"] for c in view.visible_sources()}
    namespace = view.sources[0]["chapter_id"] if view.sources else "empty"
    def node(start, size, level, parent_id=None):
        members = [c for i, c in prior if start <= i < start + size and c["chapter_id"] in visible]
        data = {"level": level, "start_position": start, "end_position": start + size - 1,
                "parent_id": parent_id, "chapter_ids": [c["chapter_id"] for c in members],
                "headers": [{k: c[k] for k in ("chapter_id", "revision_id", "match_key", "title", "order")} for c in members]}
        data["node_id"] = "ml-node:" + digest({"namespace": namespace, "config": asdict(config),
            "level": level, "start": start})[:24]
        return data
    maximum = prior[-1][0] if prior else 0
    roots, children = [], []
    for start in range(1, maximum + 1, config.parent_chapters):
        root = node(start, config.parent_chapters, 1)
        if root["chapter_ids"]:
            roots.append(root)
        for child_start in range(start, min(start + config.parent_chapters, maximum + 1), config.child_chapters):
            child = node(child_start, config.child_chapters, 2, root["node_id"])
            if child["chapter_ids"]:
                children.append(child)
    return {"version": "multilevel-index-v1", "policy": view.policy.public(), "config": asdict(config),
            "roots": roots, "maximum_position": maximum,
            "excluded_source_ids": [c["chapter_id"] for _, c in prior if c["chapter_id"] not in visible]}, children


def build_multilevel_index(view, config=None):
    """Cheap source-title catalog. No semantic child summary is constructed."""
    config = config or MultiLevelConfig()
    index, _ = _catalog(view, config)
    return {**index, "index_id": view.store.put("multilevel-index", index)}


def read_multilevel_index(view, index_id):
    index = view.store.get(index_id, "multilevel-index")
    expected, _ = _catalog(view, MultiLevelConfig(**index["config"]))
    if index != expected:
        raise NarrativeError("stale_summary", "multilevel catalog policy/headers changed")
    return index


def list_children(view, index_id, parent_id):
    index = read_multilevel_index(view, index_id)
    if parent_id not in {n["node_id"] for n in index["roots"]}:
        raise NarrativeError("visibility_denied", "unknown parent node")
    _, children = _catalog(view, MultiLevelConfig(**index["config"]))
    return [n for n in children if n["parent_id"] == parent_id]


def select_nodes(view, index_id, *, n_child=2, requested_child_ids=()):
    if type(n_child) is not int or n_child < 0:
        raise NarrativeError("invalid_record", "nonnegative child window required")
    index = read_multilevel_index(view, index_id)
    config = MultiLevelConfig(**index["config"])
    _, children = _catalog(view, config)
    by_id = {n["node_id"]: n for n in children}
    if len(set(requested_child_ids)) != len(requested_child_ids) or set(requested_child_ids) - set(by_id):
        raise NarrativeError("visibility_denied", "unknown/duplicate requested child")
    last_start = ((index["maximum_position"] - 1) // config.child_chapters) * config.child_chapters + 1
    near_starts = {last_start - i * config.child_chapters for i in range(n_child)}
    selected = [n for n in children if n["start_position"] in near_starts or n["node_id"] in requested_child_ids]
    return index["roots"] + selected


def _legacy_node_messages(node, pairs, config):
    messages = summary_messages(pairs, [], config)
    messages[0]["content"] += (
        "\n这是多级懒索引中的单个节点。只生成本节点整体摘要与关键翻译元信息；details必须为空数组。"
        "不要预先生成下级分块摘要，也不要把章节目录当已读剧情。"
        "整体摘要按揭示顺序概括主要事件、参与者、原因与结果；只写本次正文，勿补写父级或邻块剧情。"
        "人物名称/专名只收真正相关的重要项，不收少女、少年、受付嬢等泛称。"
        "不同人物、地点、技能或物品分别建条目，不因同类而归为一个实体。"
        "引文尽量短且逐字复制，译法引文必须包含所声称的原文和中文形式。"
        "只输出一个JSON，不添加解释、草稿、第二份JSON或总结。")
    messages[1]["content"] = (f"节点层级：{node['level']}；完整可见章节数：{len(node['chapter_ids'])}；"
        f"章节：{[p['source']['match_key'] for p in pairs]}\n" + messages[1]["content"])
    # Hundreds of thousands of tokens can separate the initial instructions from
    # generation. Restate the task/schema after the complete evidence, without
    # dropping any chapter or asking the model to continue the last novel line.
    messages[1]["content"] += (
        f"\n\n【本节点资料结束；现在执行摘要任务】\n上方已提供{len(pairs)}章完整日文，"
        f"其中{sum(bool(p['translation']) for p in pairs)}章带中文对照；"
        f"范围从{pairs[0]['source']['match_key']}至{pairs[-1]['source']['match_key']}。"
        "请基于上方正文总结整个可见节点，不续写最后一段小说。以下重申任务规则和JSON格式：\n"
        + messages[0]["content"] + "\n请现在只输出本节点的一份完整JSON。")
    return messages


def node_messages(node, pairs, config):
    return protocol_messages(node, pairs, config)


def _identity(node, pairs, request, provider, config, tokenizer, prompt_version=PROMPT_VERSION):
    # Do not key on the target's overall cutoff. All actual inputs are keyed and
    # subsequently re-admitted under the current view before reuse.
    return digest({"node_id": node["node_id"], "chapter_ids": node["chapter_ids"], "pairs": pairs,
                   "request": request, "provider": provider, "config": asdict(config),
                   "tokenizer": tokenizer, "prompt": prompt_version})


def _summary_cache(store):
    return {a["request_identity"]: sid for sid, a in store.objects("multilevel-summary")
            if a["prompt_version"] != PROMPT_VERSION or a.get("quality", {}).get("admission_version") == ADMISSION_VERSION}


def read_node_summary(view, summary_id, node):
    artifact = view.store.get(summary_id, "multilevel-summary")
    if (artifact["node_id"] != node["node_id"] or artifact["chapter_ids"] != node["chapter_ids"] or
            artifact["tokenizer"] != view.counter.identity or artifact["prompt_version"] not in {PROMPT_VERSION, LEGACY_PROMPT_VERSION}):
        raise NarrativeError("stale_summary", "node summary scope/version differs")
    view.archive.ensure_rows([cid for p in artifact["pairs"] for cid in
        [p["source"]["chapter_id"]]+[d["chapter_id"] for d in p["translation"]]])
    validate_pairs(view, artifact["pairs"])
    if [p["source"]["chapter_id"] for p in artifact["pairs"]] != node["chapter_ids"]:
        raise NarrativeError("stale_summary", "node pairs have different coverage")
    config = BilingualSummaryConfig(**artifact["summary_config"])
    request = artifact["request"]
    version = artifact["prompt_version"]
    make_messages = _legacy_node_messages if version == LEGACY_PROMPT_VERSION else node_messages
    if (request["messages"] != make_messages(node, artifact["pairs"], config) or artifact["request_identity"] !=
            _identity(node, artifact["pairs"], request, artifact["provider"], config, artifact["tokenizer"], version)):
        raise NarrativeError("stale_summary", "node summary request differs")
    if version == LEGACY_PROMPT_VERSION:
        validate_content(artifact["content"], artifact["pairs"], [], config)
    else:
        raw = artifact["generation"]["text"]
        decoded = decode_summary(raw, artifact["pairs"], config)
        if artifact.get("repair_id"):
            repair = view.store.get(artifact["repair_id"], "summary-format-repair")
            if (decoded["usable"] or repair["status"] != "accepted" or repair["original_output"] != raw or
                    repair["pairs_digest"] != digest(artifact["pairs"]) or
                    repair["provider"] != artifact["provider"] or
                    repair["request"]["messages"] != repair_messages(raw, decoded, config) or
                    not fidelity(raw, repair["generation"]["text"])["accepted"]):
                raise NarrativeError("stale_summary", "summary format repair provenance differs")
            decoded = decode_summary(repair["generation"]["text"], artifact["pairs"], config)
        if not decoded["usable"] or decoded["content"] != artifact["content"] or decoded["quality"] != artifact["quality"]:
            raise NarrativeError("stale_summary", "summary admission or annotations differ")
    return artifact


def plan_multilevel_summaries(view, index_id, *, provider_identity, n_child=2, requested_child_ids=(),
        target_node_ids=None, summary_config=None, context_limit=1048576, max_output_tokens=16000, safety_tokens=8192,
        max_requests=16, max_total_input=8000000, max_total_output=300000,
        max_repair_requests=2, repair_max_input=64000, repair_max_output=8192):
    config = summary_config or BilingualSummaryConfig(overview_max_chars=2500, detail_max_chars=2500,
        max_entities=20, max_fixed_expressions=5, max_quote_chars=160)
    if (min(context_limit, max_output_tokens, max_requests, max_total_input, max_total_output) <= 0 or
            safety_tokens < 0 or context_limit <= max_output_tokens + safety_tokens or
            type(max_repair_requests) is not int or max_repair_requests < 0 or min(repair_max_input, repair_max_output) <= 0):
        raise NarrativeError("budget_exceeded", "invalid lazy build budget")
    nodes = select_nodes(view, index_id, n_child=n_child, requested_child_ids=requested_child_ids)
    required_count = len(nodes)
    if target_node_ids is not None:
        if (not target_node_ids or len(set(target_node_ids)) != len(target_node_ids) or
                set(target_node_ids) - {n["node_id"] for n in nodes}):
            raise NarrativeError("visibility_denied", "unknown/duplicate/empty targeted nodes")
        nodes = [n for n in nodes if n["node_id"] in target_node_ids]
    # Load once to avoid scanning JSONL once per node/chapter.
    source_ids = {cid for n in nodes for cid in n["chapter_ids"]}
    view.archive.ensure_rows(list(source_ids) + [c["chapter_id"] for c in view.archive.chapters.values()
        if c["language"] == "zh" and view.archive.canonical(c) in source_ids])
    cached = _summary_cache(view.store)
    tasks = []
    for node in nodes:
        pairs = view.read_chapters(node["chapter_ids"])
        request = asdict(GenerationRequest(node_messages(node, pairs, config), max_tokens=max_output_tokens,
                                          temperature=0.2, task="multilevel_bilingual_summary"))
        identity = _identity(node, pairs, request, provider_identity, config, view.counter.identity)
        estimate = view.counter.messages(request["messages"])
        sid = cached.get(identity)
        if sid:
            read_node_summary(view, sid, node)
        status = "cached" if sid else "pending" if estimate + max_output_tokens + safety_tokens <= context_limit else "blocked_context"
        tasks.append({"node": node, "pairs": pairs, "request": request, "request_identity": identity,
            "estimated_input": estimate, "status": status, "cached_summary_id": sid})
    pending = [t for t in tasks if t["status"] == "pending"]
    repair_slots = min(len(pending), max_repair_requests)
    reserved_input = sum(t["estimated_input"] for t in pending)+repair_slots*repair_max_input
    reserved_output = len(pending)*max_output_tokens+repair_slots*repair_max_output
    limits = {"summary_requests": max_requests, "repair_requests": max_repair_requests,
              "translation_requests": 2, "requests": max_requests + max_repair_requests + 2,
              "input_tokens": max_total_input, "output_tokens": max_total_output}
    if len(pending) > max_requests or reserved_input > max_total_input or reserved_output > max_total_output:
        raise NarrativeError("budget_exceeded", "runnable lazy tasks exceed aggregate budget")
    plan = {"version": PROMPT_VERSION, "index_id": index_id, "policy": view.policy.public(), "provider": provider_identity,
        "tokenizer": view.counter.identity, "summary_config": asdict(config), "n_child": n_child,
        "requested_child_ids": list(requested_child_ids), "tasks": tasks, "limits": limits,
        "target_node_ids": list(target_node_ids) if target_node_ids is not None else None,
        "required_nodes": required_count,
        "context_limit": context_limit, "safety_tokens": safety_tokens,
        "repair": {"max_input_tokens": repair_max_input, "max_output_tokens": repair_max_output,
                   "maximum_requests": max_repair_requests, "conditional_slots": repair_slots},
        "pending_requests": len(pending), "blocked_requests": sum(t["status"] == "blocked_context" for t in tasks),
        "reserved_input": reserved_input, "reserved_output": reserved_output}
    plan["plan_id"] = "multilevel-plan:" + digest(plan)
    return plan


def run_multilevel_summary_plan(view, plan, provider, *, provider_identity, allow_partial=False, retry_failed=False):
    if (plan["plan_id"] != "multilevel-plan:" + digest({k: v for k, v in plan.items() if k != "plan_id"}) or
            plan["version"] != PROMPT_VERSION):
        raise NarrativeError("invalid_record", "lazy plan modified")
    if plan["policy"] != view.policy.public() or plan["provider"] != provider_identity or plan["tokenizer"] != view.counter.identity:
        raise NarrativeError("visibility_denied", "lazy plan policy/model/tokenizer differs")
    if getattr(getattr(provider, "config", None), "retries", 0):
        raise NarrativeError("budget_exceeded", "HTTP retries must be zero")
    nodes = select_nodes(view, plan["index_id"], n_child=plan["n_child"], requested_child_ids=plan["requested_child_ids"])
    required_count = len(nodes)
    if required_count != plan["required_nodes"]:
        raise NarrativeError("stale_summary", "required lazy coverage changed")
    if plan["target_node_ids"] is not None:
        if (not plan["target_node_ids"] or len(set(plan["target_node_ids"])) != len(plan["target_node_ids"]) or
                set(plan["target_node_ids"]) - {n["node_id"] for n in nodes}):
            raise NarrativeError("visibility_denied", "targeted lazy nodes outside selection")
        nodes = [n for n in nodes if n["node_id"] in plan["target_node_ids"]]
    if nodes != [t["node"] for t in plan["tasks"]]:
        raise NarrativeError("stale_summary", "planned node selection changed")
    if any(t["status"] == "blocked_context" for t in plan["tasks"]) and not allow_partial:
        raise NarrativeError("budget_exceeded", "coarse nodes exceed context; partial diagnostic execution must be explicit")
    config = BilingualSummaryConfig(**plan["summary_config"])
    # Validate the entire task manifest before the first paid request.
    view.archive.ensure_rows([cid for t in plan["tasks"] for p in t["pairs"] for cid in
        [p["source"]["chapter_id"]]+[d["chapter_id"] for d in p["translation"]]])
    for task in plan["tasks"]:
        validate_pairs(view, task["pairs"])
        request = task["request"]
        count = view.counter.messages(request["messages"])
        if (request["messages"] != node_messages(task["node"], task["pairs"], config) or count != task["estimated_input"] or
                task["request_identity"] != _identity(task["node"], task["pairs"], request, provider_identity, config, view.counter.identity)):
            raise NarrativeError("stale_summary", "planned lazy input/request changed")
        too_large = count + request["max_tokens"] + plan["safety_tokens"] > plan["context_limit"]
        if not task.get("cached_summary_id") and (task["status"] == "blocked_context") != too_large:
            raise NarrativeError("invalid_record", "lazy context classification differs")
    results = []
    for task in plan["tasks"]:
        node = task["node"]
        cached = _summary_cache(view.store)
        sid = cached.get(task["request_identity"])
        if sid:
            read_node_summary(view, sid, node)
            results.append({"node_id": node["node_id"], "summary_id": sid, "status": "cached"})
            continue
        if task["status"] == "blocked_context":
            results.append({"node_id": node["node_id"], "status": "blocked_context"})
            continue
        previous = [a for _, a in view.store.objects("multilevel-generation") if a["request_identity"] == task["request_identity"]]
        raw, seq = None, None
        try:
            if previous:
                saved = previous[0]
                generation, seq = saved["generation"], saved["attempt_seq"]
            else:
                seq = view.store.reserve(task["request_identity"], "summary", task["estimated_input"], task["request"]["max_tokens"],
                                         plan["limits"], retry_failed=retry_failed)
                raw = provider.generate(GenerationRequest(**task["request"]))
                generation = asdict(raw)
                view.store.put("multilevel-generation", {"request_identity": task["request_identity"],
                    "request": task["request"], "provider": provider_identity, "generation": generation, "attempt_seq": seq})
            if generation["finish_reason"] != "stop":
                raise NarrativeError("invalid_summary", "node output did not finish normally")
            decoded = decode_summary(generation["text"], task["pairs"], config)
            repair = {"status": "not_needed"}
            if not decoded["usable"] and plan["repair"]["maximum_requests"]:
                repair = repair_summary(view, generation["text"], decoded, task["pairs"], config, provider,
                    provider_identity=provider_identity, limits=plan["limits"],
                    max_input_tokens=plan["repair"]["max_input_tokens"], max_output_tokens=plan["repair"]["max_output_tokens"],
                    context_limit=plan["context_limit"], safety_tokens=plan["safety_tokens"])
                decoded = repair["decoded"]
            if not decoded["usable"]:
                view.store.finish(seq, "failed", {"raw": generation, "usage": generation["usage"],
                    "diagnosis": decoded, "repair_id": repair.get("repair_id"), "repair_status": repair["status"]})
                results.append({"node_id": node["node_id"], "status": "failed", "quality": decoded["quality"],
                                "repair_status": repair["status"], "repair_id": repair.get("repair_id")})
                break
            artifact = {"node_id": node["node_id"], "chapter_ids": node["chapter_ids"], "pairs": task["pairs"],
                "content": decoded["content"], "quality": decoded["quality"], "repair_id": repair.get("repair_id"),
                "request": task["request"], "request_identity": task["request_identity"],
                "provider": provider_identity, "tokenizer": view.counter.identity, "summary_config": asdict(config),
                "prompt_version": PROMPT_VERSION, "generation": generation, "origin_policy": plan["policy"],
                "review_status": "reference_candidate_semantics_unreviewed"}
            sid = view.store.put("multilevel-summary", artifact)
            view.store.finish(seq, "completed", {"summary_id": sid, "raw": generation, "usage": generation["usage"],
                                               "repair_id": repair.get("repair_id")})
            results.append({"node_id": node["node_id"], "summary_id": sid, "status": "generated",
                            "repair_status": repair["status"], "reference_status": decoded["quality"]["reference_status"]})
        except (NarrativeError, ProviderError) as exc:
            if seq is None:
                raise
            view.store.finish(seq, "unknown_remote_result" if getattr(exc, "kind", None) in {"timeout", "network"} else "failed",
                              {"error": str(exc), "raw": asdict(raw) if raw else None,
                               "provider_details": getattr(exc, "details", {})})
            results.append({"node_id": node["node_id"], "status": "failed", "error": str(exc)})
            break
    return {"plan_id": plan["plan_id"], "results": results,
            "complete": len(results) == len(plan["tasks"]) and all(r["status"] in {"cached", "generated"} for r in results),
            "all_required_nodes_built": len(results) == required_count and all(r["status"] in {"cached", "generated"} for r in results)}


def rollup_multilevel_metadata(view, index_id, summary_ids, *, persist=True):
    """Union evidence into new parent revisions. Missing children stay missing."""
    index = read_multilevel_index(view, index_id)
    _, children = _catalog(view, MultiLevelConfig(**index["config"]))
    nodes = {n["node_id"]: n for n in index["roots"] + children}
    if set(summary_ids) - set(nodes) or len(set(summary_ids.values())) != len(summary_ids):
        raise NarrativeError("visibility_denied", "unknown/duplicate metadata input")
    artifacts = {nid: read_node_summary(view, sid, nodes[nid]) for nid, sid in summary_ids.items()}
    parents = []
    for root in index["roots"]:
        inputs = [n for n in [root]+children if n["node_id"] in artifacts and
                  (n["node_id"] == root["node_id"] or n["parent_id"] == root["node_id"])]
        entries, expressions, conflicts = [], [], []
        detailed = set()
        for node in inputs:
            sid = summary_ids[node["node_id"]]
            meta = artifacts[node["node_id"]]["content"]["translation_metadata"]
            if node["level"] == 2:
                detailed.update(node["chapter_ids"])
            for i, local in enumerate(meta["entities"]):
                quality = artifacts[node["node_id"]].get("quality")
                annotation = {"quality": component_quality(quality, "/translation_metadata/entities/"+str(i))} if quality else {}
                if quality:
                    local = {**local, "renderings": [{**r, "quality": component_quality(quality,
                        f"/translation_metadata/entities/{i}/renderings/{j}")} for j, r in enumerate(local["renderings"])]}
                entries.append({**local, "entry_id": "ml-entity:"+digest({"summary": sid, "local": local["local_entity_id"]}),
                    **annotation,
                    "block_id": node["node_id"], "summary_id": sid, "source_level": node["level"],
                    "source_chapter_ids": node["chapter_ids"], "status": "observed", "review_status": "semantics_unreviewed"})
            expressions.extend({**r, "status": "observed", "summary_id": sid,
                **({"quality": component_quality(artifacts[node["node_id"]]["quality"], "/translation_metadata/fixed_expressions/"+str(i))}
                   if "quality" in artifacts[node["node_id"]] else {})} for i, r in enumerate(meta["fixed_expressions"]))
            conflicts.extend({"summary_id": sid, "text": c} for c in meta["conflicts"])
        data = {"parent_id": root["node_id"], "chapter_ids": root["chapter_ids"],
            "coarse_summary_id": summary_ids.get(root["node_id"]),
            "inputs": [{"node_id": n["node_id"], "summary_id": summary_ids[n["node_id"]]} for n in inputs],
            "detailed_chapter_ids": [c for c in root["chapter_ids"] if c in detailed],
            "unbuilt_child_ids": [n["node_id"] for n in children if n["parent_id"] == root["node_id"] and n["node_id"] not in artifacts],
            "entries": entries, "fixed_expressions": expressions, "conflicts": conflicts, "identity_links": []}
        pid = view.store.put("parent-metadata", data) if persist else "parent-metadata:"+digest(data)
        parents.append({**data, "snapshot_id": pid})
    result = {"version": "multilevel-metadata-v1", "index_id": index_id, "policy": view.policy.public(),
              "summary_ids": dict(summary_ids), "parents": parents, "identity_links": []}
    sid = view.store.put("multilevel-metadata", result) if persist else "multilevel-metadata:"+digest(result)
    return {**result, "snapshot_id": sid}
