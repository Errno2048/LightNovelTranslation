"""Explicit adapter for accepted generated text, retaining policy/dependency closure."""
from ..narrative.schemas import NarrativeError, NarrativePolicy
from ..narrative.service import NarrativeView
from ..storage import digest
from .acceptance import current_head
from .versions import read_version


def _accepted_version(view, source_id, acceptance_id):
    source = view.archive.chapters.get(source_id)
    if not source: raise NarrativeError("visibility_denied", "unknown accepted source")
    view.check(source, translation=True)
    if current_head(view.store, source_id) != acceptance_id:
        raise NarrativeError("stale_summary", "generated acceptance revoked or superseded")
    a = view.store.get(acceptance_id, "translation-acceptance")
    v = view.store.get(a["version_id"], "translation-version")
    if a["source_id"] != source_id or v["source_id"] != source_id:
        raise NarrativeError("invalid_record", "acceptance source differs")
    # Original generation uses its original policy; additionally check its entire
    # dependency closure under the current consumer policy before exposing text.
    context = view.store.get(v["context_id"], "review-context")
    for dep in context["dependencies"] + v.get("additional_dependencies", []):
        if dep["chapter_id"].startswith("generated:"):
            validate_accepted_dependency(view, dep)
        else:
            view.validate_dependency(dep)
    original = NarrativeView(view.archive, view.store, NarrativePolicy(**v["policy"]), counter=view.counter)
    return a, read_version(original, a["version_id"])


def read_accepted_pair(view, source_id):
    aid = current_head(view.store, source_id)
    if not aid: raise NarrativeError("coverage_gap", "chapter has no active accepted generated text")
    a, v = _accepted_version(view, source_id, aid)
    source = view.body(source_id)
    doc = {"chapter_id": "generated:"+aid, "revision_id": v["text_hash"], "language": "zh", "source": "generated_accepted",
        "match_key": source["match_key"], "order": source["order"], "title": source["title"],
        "text": v["text"], "text_hash": digest(v["text"]), "paragraph_ids": [s["id"] for s in v["segments"]],
        "paragraph_texts": [s["text"] for s in v["segments"]], "acceptance_id": aid, "canonical_source_id": source_id,
        "coverage_id": view.store.get(a["plan_id"], "translation-acceptance-plan")["coverage_id"]}
    return {"source": source, "translation": [doc], "translation_status": "generated_accepted",
            "review_ids": [aid], "acceptance_id": aid}


def validate_accepted_dependency(view, dep):
    aid = dep.get("acceptance_id")
    if not aid or not dep.get("canonical_source_id"):
        raise NarrativeError("invalid_record", "generated dependency requires acceptance and canonical source")
    actual = read_accepted_pair(view, dep["canonical_source_id"])["translation"][0]
    if any(dep.get(k) != actual[k] for k in ("chapter_id", "revision_id", "paragraph_ids", "text_hash")) or dep.get("title_hash") != digest(actual["title"]):
        raise NarrativeError("stale_summary", "generated dependency changed")
    return actual


class AcceptedNarrativeView(NarrativeView):
    """Opt in to generated accepted near chapters; raw corpus readers stay unchanged."""
    def __init__(self, *args, generated_source_ids=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.generated_source_ids = frozenset(generated_source_ids)

    def read_chapters(self, ids):
        return [read_accepted_pair(self, cid) if cid in self.generated_source_ids else super(AcceptedNarrativeView, self).read_chapters([cid])[0] for cid in ids]

    def summary_view(self):
        # Historical summaries retain the exact original bilingual inputs. The
        # accepted overlay applies to near chapters, not retroactively to roots.
        return NarrativeView(self.archive, self.store, self.policy, counter=self.counter, chapter_reviews=self.chapter_reviews)

    def body(self, chapter_id, *, task=False):
        if chapter_id.startswith("generated:"):
            aid = chapter_id[len("generated:"):]
            a = self.store.get(aid, "translation-acceptance")
            return read_accepted_pair(self, a["source_id"])["translation"][0]
        return super().body(chapter_id, task=task)

    def validate_dependency(self, dep, *, task=False):
        if dep["chapter_id"].startswith("generated:"):
            return validate_accepted_dependency(self, dep)
        return super().validate_dependency(dep, task=task)
