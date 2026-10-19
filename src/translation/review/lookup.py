"""Bounded, policy-checked evidence reads. A read never silently pays for a summary."""
from ..narrative.schemas import NarrativeError
from ..narrative.service import dependency
from ..storage import digest
from .versions import read_version, source_segments


def resolve_review_queries(view, version_id, queries, *, max_queries=2):
    v = read_version(view, version_id)
    if len(queries) > max_queries or len(set(queries)) != len(queries):
        raise NarrativeError("budget_exceeded", "at most two distinct lookup actions")
    root = version_id
    ancestor = v
    while ancestor.get("parent"):
        root = ancestor["parent"]
        ancestor = read_version(view, root)
    prior = [(ident, a) for ident, a in view.store.objects("review-lookup") if a.get("root_version", a["version_id"]) == root]
    existing = {a["query"]: (ident, a) for ident, a in prior if a["version_id"] == version_id}
    if len({a["query"] for _, a in prior} | set(queries)) > max_queries:
        raise NarrativeError("budget_exceeded", "persisted lookup action limit exceeded")
    results = []
    context = view.store.get(v["context_id"], "review-context")
    for query in queries:
        if query in existing:
            ident, data = existing[query]
            for dep in data["dependencies"]: view.validate_dependency(dep)
            results.append({"id": ident, **data}); continue
        data = {"version_id": version_id, "root_version": root, "query": query, "status": "not_found", "documents": [], "dependencies": [],
                "policy_fingerprint": view.policy.fingerprint, "summary": None}
        try:
            if query.startswith("chapter:"):
                key = query[len("chapter:"):]
                source = view.archive.sources.get(key)
                if source:
                    view.check(source)  # Target source is forbidden through this prior-history tool.
                    pair = view.read_chapters([source["chapter_id"]])[0]
                    data["status"] = "found"
                    for doc in [pair["source"]]+pair["translation"]:
                        data["documents"].append(doc)
                        data["dependencies"].append(dependency(view.body(doc["chapter_id"])))
                    data["translation_status"] = pair["translation_status"]
            elif query.startswith("node:") and context.get("index_id"):
                from ..narrative.multilevel import read_multilevel_index, list_children, read_node_summary
                node_id = query[len("node:"):]
                index = read_multilevel_index(view, context["index_id"])
                nodes = index["roots"]+sum([list_children(view, context["index_id"], r["node_id"]) for r in index["roots"]], [])
                matches = [n for n in nodes if n["node_id"] == node_id]
                if matches:
                    data["status"] = "not_built"
                    selected = [s for s in context["selected_summaries"] if s["block_id"] == node_id]
                    if not selected:
                        builds = [a for _, a in view.store.objects("review-detail-build") if a["version_id"] == version_id and a["node_id"] == node_id and a["result"]["complete"]]
                        if builds:
                            selected = [{"summary_id": builds[0]["result"]["results"][0]["summary_id"]}]
                    if selected:
                        artifact = read_node_summary(view, selected[0]["summary_id"], matches[0])
                        data.update(status="found", summary={"summary_id": selected[0]["summary_id"],
                            "node_id": node_id, "content": artifact["content"], "semantic_status": "unreviewed"})
                        data["dependencies"] = [dependency(d) for p in artifact["pairs"] for d in [p["source"]]+p["translation"]]
            elif query.startswith("node:"):
                data['reason'] = 'no_index_in_frozen_translation_context'
        except NarrativeError as exc:
            if exc.kind not in {"visibility_denied", "quality_hold"}: raise
            data.update(status="denied", reason=exc.kind, documents=[], dependencies=[], summary=None)
        if view.counter.count(str(data)) > 60000:
            data.update(status="budget_exceeded", documents=[], dependencies=[], summary=None)
        ident = view.store.put("review-lookup", data)
        results.append({"id": ident, **data})
    return results


def lookup_evidence(view, version_id, lookup_ids):
    rows, data = [], []
    for i, ident in enumerate(lookup_ids, 1):
        item = view.store.get(ident, "review-lookup")
        if item["version_id"] != version_id or item["policy_fingerprint"] != view.policy.fingerprint:
            raise NarrativeError("visibility_denied", "lookup belongs to another policy/version")
        for dep in item["dependencies"]: view.validate_dependency(dep)
        value = {"lookup_id": ident, "status": item["status"], "query": item["query"], "summary": item["summary"], "documents": []}
        for j, doc in enumerate(item["documents"], 1):
            segments, _ = source_segments(doc)
            for k, row in enumerate(segments, 1):
                row["alias"] = f"L{i}D{j}P{k:03d}"
                row["chapter_id"] = doc["chapter_id"]
            rows += segments
            value["documents"].append({"chapter_id": doc["chapter_id"], "language": doc["language"], "rows": segments})
        data.append(value)
    return rows, data


def build_review_detail(session, version_id, node_id):
    """Explicit paid action, at most one child. No automatic build on read misses."""
    from ..narrative.multilevel import plan_multilevel_summaries, run_multilevel_summary_plan
    v = read_version(session.view, version_id)
    context = session.view.store.get(v["context_id"], "review-context")
    old = session.view.store.objects("review-detail-build")
    matches = [a for _, a in old if a["version_id"] == version_id]
    if matches:
        if matches[0]["node_id"] != node_id:
            raise NarrativeError("budget_exceeded", "only one child may be built per review version")
        return matches[0]
    planned = [a for _, a in session.view.store.objects("review-detail-plan") if a["version_id"] == version_id]
    if planned and planned[0]["node_id"] != node_id:
        raise NarrativeError("budget_exceeded", "one detail build already reserved")
    plan = plan_multilevel_summaries(session.view, context["index_id"], provider_identity=session.provider_identity,
        requested_child_ids=[node_id], target_node_ids=[node_id], max_output_tokens=16384,
        max_requests=1, max_repair_requests=0, max_total_input=session.limits["input_tokens"], max_total_output=session.limits["output_tokens"])
    if plan["tasks"][0]["node"]["level"] != 2:
        raise NarrativeError("visibility_denied", "review can build level 2 only")
    plan["limits"] = session.limits
    plan["plan_id"] = "multilevel-plan:"+digest({k: val for k, val in plan.items() if k != "plan_id"})
    session.view.store.put("review-detail-plan", {"version_id": version_id, "node_id": node_id, "plan": plan})
    result = run_multilevel_summary_plan(session.view, plan, session.provider, provider_identity=session.provider_identity)
    data = {"version_id": version_id, "node_id": node_id, "result": result,
            "context_update": "explicit_new_context_required; original context unchanged"}
    session.view.store.put("review-detail-build", data)
    return data
