"""Frozen context translation with durable request/token reservations and resume."""
from dataclasses import asdict

from ..context.hierarchical import explain_context, _validate_examples, validate_knowledge, compile_hierarchical_context
from ..providers import ProviderError
from ..schemas import GenerationRequest
from ..storage import digest, read_json, write_json
from ..workflow import initial_checks
from .schemas import NarrativeError

DEFAULT_LIMITS = {"summary_requests": 12, "translation_requests": 12, "requests": 24,
                  "input_tokens": 2000000, "output_tokens": 150000}


def run_translation(view, bundle, provider, *, provider_identity, variant, output, limits=None, dry_run=False, retry_failed=False):
    explain_context(bundle)
    if view.policy.public() != bundle["policy"] or view.counter.identity != bundle["token_accounting"]["method"]:
        raise NarrativeError("visibility_denied", "translation policy/tokenizer changed")
    # Recheck boundary/summary reviews as well as sources before any new request.
    # A newly rejected review must not be bypassed by an older compiled bundle.
    from .schemas import ContextSpec
    metadata_args = ({"translation_metadata": bundle["translation_metadata"],
                      "metadata_core_forms": bundle["metadata_core_forms"]} if "translation_metadata" in bundle else {})
    if bundle.get("version") == "multilevel-context-v1":
        from ..context.multilevel import compile_multilevel_context
        from .multilevel import MultiLevelContextSpec
        rebuilt = compile_multilevel_context(view, bundle["index_id"],
            {s["block_id"]: s["summary_id"] for s in bundle["selected_summaries"]},
            spec=MultiLevelContextSpec(**bundle["requested"]), knowledge=bundle.get("knowledge"), examples=bundle.get("examples", []),
            metadata=bundle["multilevel_metadata"], metadata_core_forms=bundle["metadata_core_forms"],
            coverage_scope_reviews=bundle.get('coverage_scope_reviews',()),
            coverage_compression_reviews=bundle.get('coverage_compression_reviews',()))
    else:
        rebuilt = compile_hierarchical_context(view, bundle["index_id"],
            {s["block_id"]: s["summary_id"] for s in bundle["selected_summaries"]},
            spec=ContextSpec(**bundle["requested"]), knowledge=bundle.get("knowledge"), examples=bundle.get("examples", []), **metadata_args)
    if rebuilt != bundle:
        raise NarrativeError("stale_summary", "compiled context no longer matches current sources/reviews")
    for dep in bundle["dependencies"]:
        view.validate_dependency(dep, task=dep["chapter_id"] == view.policy.target_id)
    _validate_examples(view, bundle.get("examples", []))
    validate_knowledge(view, bundle.get("knowledge", {}))
    from .service import select_segment
    for segment in bundle.get("translation_evidence", []):
        body = view.body(segment["chapter_id"])
        actual = body if body.get("source") == "generated_accepted" else select_segment(body, segment["paragraph_ids"], view.archive)
        if actual != segment:
            raise NarrativeError("stale_summary", "translation evidence changed")
    if getattr(getattr(provider, "config", None), "retries", 0):
        raise NarrativeError("budget_exceeded", "HTTP retries must be zero for ledger accounting")
    request = GenerationRequest(bundle["messages"], max_tokens=bundle["requested"]["max_output_tokens"], task="translation")
    identity = digest({"request": asdict(request), "bundle": bundle["fingerprint"], "provider": provider_identity, "variant": variant})
    path = output / "translation.json"
    if path.exists():
        result = read_json(path)
        if result["identity"] != identity:
            raise NarrativeError("conflict", "output path already contains a different run")
        return {"status": "cached", "identity": identity}
    plan = {"identity": identity, "variant": variant, "bundle": bundle["fingerprint"], "provider": provider_identity,
            "input_reserved": view.counter.messages(request.messages), "output_reserved": request.max_tokens,
            "limits": limits or DEFAULT_LIMITS}
    if dry_run:
        return {"status": "dry_run", **plan}
    seq = view.store.reserve(identity, "translation", plan["input_reserved"], request.max_tokens, plan["limits"], retry_failed=retry_failed)
    write_json(output / "request.json", {**plan, "request": asdict(request), "policy": view.policy.public()})
    try:
        result = provider.generate(request)
        if result.finish_reason != "stop" or not result.text.strip():
            raise NarrativeError("invalid_translation", "output did not finish normally")
        artifact = {**asdict(result), **plan, "source_id": view.policy.target_id, "status": "draft_for_review",
                    "checks": initial_checks(result.text, bundle["source"]["text"])}
        write_json(path, artifact)
        view.store.finish(seq, "completed", {"output": str(path), "usage": result.usage, "elapsed_seconds": result.elapsed_seconds})
        return {"status": "drafted", **plan, "usage": result.usage, "elapsed_seconds": result.elapsed_seconds}
    except (ProviderError, NarrativeError) as exc:
        view.store.finish(seq, "unknown_remote_result" if getattr(exc, "kind", None) in {"timeout", "network"} else "failed", {
            "error": str(exc), "provider_details": getattr(exc, "details", {})})
        raise
