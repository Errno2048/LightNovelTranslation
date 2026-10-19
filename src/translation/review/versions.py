"""Immutable text versions; never treat a generated file as accepted context."""
from ..storage import digest
from ..narrative.schemas import NarrativeError
from ..narrative.service import dependency
from ..references import STABLE, chapter_ref


def revision_limit(view):
    if hasattr(view, 'repo') and hasattr(view.repo, 'revision_limit'):
        return view.repo.revision_limit(view.policy.target_id, view.repo.manifest['spec']['max_revisions'])
    return 2


def segment_translation(text, source_revision, *, chapter=None):
    if not isinstance(text, str):
        raise NarrativeError("invalid_record", "translation must be text")
    prefix = "generated:" + digest({"text": text, "source": source_revision})[:24]
    result, offset = [], 0
    for i, line in enumerate(text.splitlines(keepends=True)):
        body = line.rstrip("\r\n")
        if body.strip():
            result.append({"id": f"{prefix}:p{i:04d}", "alias": f"T{len(result)+1:03d}",
                           "text": body, "start": offset, "end": offset+len(body)})
            if chapter:
                result[-1]['alias'] = f'{chapter}.Z{len(result):03d}'
        offset += len(line)
    return result


def source_segments(source, *, reference_profile='legacy'):
    result, exempt = [], []
    for pid, text in zip(source["paragraph_ids"], source["paragraph_texts"]):
        if not text.strip():
            exempt.append({"id": pid, "reason": "whitespace_only"})
        else:
            result.append({"id": pid, "alias": f"S{len(result)+1:03d}", "text": text})
            if reference_profile == STABLE:
                tag = 'J' if source['language'] == 'ja' else 'Z'
                result[-1]['alias'] = f'{chapter_ref(source)}.{tag}{len(result):03d}'
    return result, exempt


def validate_context(view, context):
    if context.get("fingerprint") != digest({k: v for k, v in context.items() if k != "fingerprint"}):
        raise NarrativeError("stale_summary", "context fingerprint differs")
    if context["policy"] != view.policy.public():
        raise NarrativeError("visibility_denied", "review policy differs from frozen context")
    for selected in context.get("selected_summaries", []):
        summary = view.store.get(selected["summary_id"], "multilevel-summary")
        if selected.get("block_id") and summary.get("node_id") != selected["block_id"]:
            raise NarrativeError("stale_summary", "selected summary node identity changed")
    for reference in context.get("source_artifacts", []):
        artifact = view.store.get(reference["artifact_id"], "review-source-artifact")
        if (artifact.get("origin") != reference.get("origin") or
                artifact.get("sha256") != reference.get("sha256")):
            raise NarrativeError("stale_summary", "review source artifact identity changed")
        matching = [block for block in context["prompt_blocks"]
                    if block.get("origin") == reference.get("origin")]
        if len(matching) != 1 or matching[0].get("text") != artifact.get("text"):
            raise NarrativeError("stale_summary", "review source artifact differs from prompt block")
    ids = [d["chapter_id"] for d in context["dependencies"] if not d["chapter_id"].startswith("generated:")]
    view.archive.ensure_rows(ids)
    for dep in context["dependencies"]:
        if dep["chapter_id"].startswith("generated:"):
            validate_generated_dependency(view, dep)
        else:
            view.validate_dependency(dep, task=dep["chapter_id"] == view.policy.target_id)
    if context["source"] != view.body(view.policy.target_id, task=True):
        raise NarrativeError("stale_summary", "target source changed")


def validate_additional_dependencies(view, dependencies):
    view.archive.ensure_rows([d["chapter_id"] for d in dependencies if not d["chapter_id"].startswith("generated:")])
    for dep in dependencies:
        if dep["chapter_id"].startswith("generated:"):
            validate_generated_dependency(view, dep)
        else:
            view.validate_dependency(dep)


def validate_generated_dependency(view, dep):
    if dep.get('commit_id'):
        if not hasattr(view,'validate_generated_dependency'):
            raise NarrativeError('visibility_denied','branch output requires a sequence view')
        return view.validate_generated_dependency(dep)
    from .accepted_view import validate_accepted_dependency
    return validate_accepted_dependency(view,dep)


def create_version(view, context, text, *, finish_reason="stop", parent=None, revision_id=None,
                   additional_dependencies=(), output_generation=None):
    validate_context(view, context)
    source = context["source"]
    if parent:
        previous = read_version(view, parent)
        if previous["context_fingerprint"] != context["fingerprint"]:
            raise NarrativeError("conflict", "revision must explicitly preserve its frozen context")
        if previous["round"] >= revision_limit(view):
            raise NarrativeError("budget_exceeded", "semantic revision budget used")
    else:
        previous = None
    known = {d["chapter_id"]: d for d in context["dependencies"]}
    extra = {}
    for dep in (previous or {}).get("additional_dependencies", []) + list(additional_dependencies):
        cid = dep["chapter_id"]
        if cid in known and known[cid] != dep or cid in extra and extra[cid] != dep:
            raise NarrativeError("conflict", "additional dependency version conflicts with frozen inputs")
        if cid not in known:
            extra[cid] = dep
    additional = [extra[cid] for cid in sorted(extra)]
    validate_additional_dependencies(view, additional)
    profile = context.get('reference_profile', 'legacy')
    if profile not in {'legacy', STABLE}:
        raise NarrativeError('invalid_record', 'unknown reference profile')
    rows, exempt = source_segments(source, reference_profile=profile)
    ctxid = view.store.put("review-context", context)
    data = {"schema": "translation-version-v1", "source_id": source["chapter_id"],
            "source_revision": source["revision_id"], "source_dependency": dependency(source),
            "source_segments": rows, "layout_exempt": exempt, "text": text, "text_hash": digest(text),
            "segments": segment_translation(text, source["revision_id"],
                chapter=chapter_ref(source) if profile==STABLE else None), "finish_reason": finish_reason,
            "context_id": ctxid, "context_fingerprint": context["fingerprint"],
            "policy": view.policy.public(), "parent": parent, "revision_id": revision_id,
            "round": previous["round"]+1 if previous else 0, "initial_state": "draft_for_review"}
    if additional:
        data["additional_dependencies"] = additional
    if profile == STABLE:
        data['reference_profile'] = STABLE
    if output_generation:
        data['output_binding'] = {'profile': 'source-records-v1', 'generation_id': output_generation,
            'authority': 'declared_by_translation_output', 'semantic_status': 'not_verified'}
    elif previous and previous.get('output_binding'):
        data['output_binding'] = {**previous['output_binding'], 'inherited_from': parent}
    if data.get('output_binding'):
        validate_output_binding(view, data, context)
    return view.store.put("translation-version", data)


def validate_output_binding(view, value, context):
    """A record ID is an auditable declaration, never semantic proof."""
    from ..sequence.output import parse_output, PROFILE
    binding = value['output_binding']
    if (binding.get('profile') != PROFILE or context.get('output_profile') != PROFILE or
            binding.get('authority') != 'declared_by_translation_output' or
            binding.get('semantic_status') != 'not_verified' or
            len(value['source_segments']) != len(value['segments'])):
        raise NarrativeError('invalid_record', 'invalid source-bound translation binding')
    if binding.get('inherited_from'):
        if binding['inherited_from'] != value.get('parent'):
            raise NarrativeError('invalid_record', 'output binding inherited from a different version')
        previous = read_version(view, binding['inherited_from'])
        expected = {**previous.get('output_binding', {}), 'inherited_from': binding['inherited_from']}
        if binding != expected:
            raise NarrativeError('invalid_record', 'inherited source binding differs')
    else:
        if value.get('parent'):
            raise NarrativeError('invalid_record', 'revised output requires an inherited binding')
        raw = view.store.get(binding['generation_id'], 'sequence-response')
        request = view.store.get(raw['request_object_id'], 'sequence-request')
        if (request['kind'] != 'translation' or
                request['messages'][:len(context['messages'])] != context['messages'] or
                raw['generation']['finish_reason'] != 'stop'):
            raise NarrativeError('conflict', 'bound output belongs to another frozen request')
        parsed = parse_output(raw['generation']['text'], value['source_segments'])
        if not parsed['complete'] or parsed['text'] != value['text']:
            raise NarrativeError('invalid_record', 'bound output differs from original generation')


def read_version(view, version_id):
    value = view.store.get(version_id, "translation-version")
    context = view.store.get(value["context_id"], "review-context")
    validate_context(view, context)
    validate_additional_dependencies(view, value.get("additional_dependencies", []))
    profile = value.get('reference_profile', 'legacy')
    rows, exempt = source_segments(context["source"], reference_profile=profile)
    if (value["policy"] != view.policy.public() or value["source_id"] != context["source"]["chapter_id"]
            or value["source_revision"] != context["source"]["revision_id"]
            or value["text_hash"] != digest(value["text"]) or value["source_segments"] != rows
            or value["layout_exempt"] != exempt or value["context_fingerprint"] != context["fingerprint"]
            or value["source_dependency"] != dependency(context["source"])
            or profile != context.get('reference_profile', 'legacy')
            or value["segments"] != segment_translation(value["text"], value["source_revision"],
                chapter=chapter_ref(context['source']) if profile==STABLE else None)):
        raise NarrativeError("stale_summary", "translation version binding changed")
    if value.get('output_binding'):
        validate_output_binding(view, value, context)
    return value
