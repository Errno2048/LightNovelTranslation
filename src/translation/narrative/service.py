"""Permission-bound body readers, title-driven blocks and explicit full-range reviews."""
from dataclasses import asdict
import re

from ..knowledge.schemas import QueryPolicy
from ..storage import digest
from .schemas import BlockConfig, NarrativeError, NarrativePolicy, VERSION
from .tokens import TokenCounter


class NarrativeView:
    def __init__(self, archive, store, policy: NarrativePolicy, *, counter=None, chapter_reviews=()):
        self.archive, self.store, self.policy = archive, store, policy
        self.counter = counter or TokenCounter()
        self.chapter_reviews = list(chapter_reviews)
        self.sources = sorted((c for c in archive.chapters.values() if c["language"] == "ja" and c["kind"] == "narrative"),
                              key=lambda c: (c["order"], c["chapter_id"]))
        if policy.target_id is not None:
            target = archive.chapters.get(policy.target_id, {})
            if target.get("language") != "ja" or target.get("kind") != "narrative" or policy.cutoff_order != target["order"] - 1:
                raise NarrativeError("unsupported_policy", "target requires its immediately preceding reading-order cutoff")
            if policy.target_id in policy.reserved_source_ids:
                raise NarrativeError("visibility_denied", "cannot translate reserved target in development")

    def check(self, chapter, *, task=False, translation=False):
        cid = self.archive.canonical(chapter)
        if cid is None or cid in self.policy.reserved_source_ids:
            raise NarrativeError("visibility_denied", "reserved or unmapped source")
        source = self.archive.chapters[cid]
        if self.policy.cutoff_order is not None and source["order"] > self.policy.cutoff_order:
            if not (task and not translation and cid == self.policy.target_id):
                raise NarrativeError("visibility_denied", "not in visible prior history")
        if translation:
            if cid == self.policy.target_id or cid in self.policy.hidden_translation_source_ids:
                raise NarrativeError("visibility_denied", "hidden reference translation")
            allow = self.policy.allowed_translation_source_ids
            if allow is not None and cid not in allow:
                raise NarrativeError("visibility_denied", "translation outside allowlist")
        # Retain the crawler/paragraph quality holds rather than bypassing P4 admission.
        self.archive.check_allowed(chapter, QueryPolicy("single_gap", reserved_source_ids=self.policy.reserved_source_ids))

    def visible_sources(self):
        return [c for c in self.sources if c["chapter_id"] not in self.policy.reserved_source_ids
                and (self.policy.cutoff_order is None or c["order"] <= self.policy.cutoff_order)]

    def validate_source_dependencies(self, ids, *, derived_bilingual=False):
        """Check the declared dependency closure, including sources absent from quotes.

        P4 dependency IDs are canonical Japanese IDs and lose language information.
        Bilingual derivatives therefore conservatively inherit translation restrictions.
        Source-only summaries use their exact Japanese document dependencies instead.
        """
        if not isinstance(ids, list) or any(not isinstance(cid, str) for cid in ids):
            raise NarrativeError("invalid_record", "explicit source dependency list required")
        for cid in ids:
            source = self.archive.chapters.get(cid)
            if not source or source["language"] != "ja":
                raise NarrativeError("visibility_denied", "unknown/noncanonical source dependency")
            self.check(source)
            if derived_bilingual:
                if cid == self.policy.target_id or cid in self.policy.hidden_translation_source_ids:
                    raise NarrativeError("visibility_denied", "hidden translation dependency")
                allow = self.policy.allowed_translation_source_ids
                if allow is not None and cid not in allow:
                    raise NarrativeError("visibility_denied", "dependency outside translation allowlist")

    def body(self, chapter_id, *, task=False):
        c = self.archive.chapters[chapter_id]
        self.check(c, task=task, translation=c["language"] == "zh")
        self.archive.ensure_rows([chapter_id])
        rows = [p for p in self.archive.rows[chapter_id] if p["kind"] == "body"]
        if not rows or any(p.get("section", {}).get("semantic_status") == "known_numeric_conflict" for p in rows):
            raise NarrativeError("quality_hold", "empty or conflicting chapter body")
        text = "\n".join(p["text"] for p in rows)
        return {"chapter_id": chapter_id, "revision_id": c["revision_id"], "language": c["language"],
                "match_key": c.get("match_key"), "order": c["order"], "title": c["title"],
                "paragraph_ids": [p["paragraph_id"] for p in rows], "paragraph_texts": [p["text"] for p in rows],
                "text": text, "text_hash": digest(text)}

    def validate_dependency(self, dep, *, task=False):
        actual = self.body(dep["chapter_id"], task=task)
        if any(dep.get(k) != actual[k] for k in ("revision_id", "text_hash", "paragraph_ids")) or dep.get("title_hash") != digest(actual["title"]):
            raise NarrativeError("stale_summary", "source revision/body changed")
        return actual

    def list_blocks(self, index_id):
        index = self.store.get(index_id, "index")
        expected = [c["chapter_id"] for c in self.visible_sources()]
        if index["policy"] != self.policy.public() or index["chapter_ids"] != expected:
            raise NarrativeError("visibility_denied", "index is not scoped to this exact prior-history policy")
        if [c for b in index["blocks"] for c in b["chapter_ids"]] != expected:
            raise NarrativeError("invalid_record", "block partition has missing, duplicate or reordered chapters")
        if [d["chapter_id"] for d in index["dependencies"]] != expected:
            raise NarrativeError("invalid_record", "index dependency coverage differs")
        for b in index["blocks"]:
            if [c for r in b["ranges"] for c in r["chapter_ids"]] != b["chapter_ids"]:
                raise NarrativeError("invalid_record", "detailed ranges are not an exact ordered partition")
        # Loading one missing chapter at a time would scan the entire JSONL corpus
        # once per chapter on cache resume (1357 scans for the production index).
        self.archive.ensure_rows(expected)
        for dep in index["dependencies"]:
            self.validate_dependency(dep)
        for row in index.get("boundary_reviews", []) + index.get("boundary_overrides", []):
            for dep in row["evidence"]:
                self.validate_dependency(dep)
        return index

    def read_summary(self, summary_id, chapter_ids):
        summary = self.store.get(summary_id, "summary")
        if summary["chapter_ids"] != chapter_ids:
            raise NarrativeError("visibility_denied", "summary is not for the exact visible block/prefix")
        if summary.get("schema_version") == "bilingual-block":
            from .bilingual import validate_artifact
            validate_artifact(self, summary)
            return summary
        if len(summary["documents"]) != len(chapter_ids) or len(summary["dependencies"]) != len(chapter_ids):
            raise NarrativeError("invalid_summary", "summary source coverage differs")
        for cid, doc, dep in zip(chapter_ids, summary["documents"], summary["dependencies"]):
            actual = self.validate_dependency(dep)
            if cid != doc["chapter_id"] or doc != actual:
                raise NarrativeError("stale_summary", "summary input does not match complete visible body")
        from .summaries import validate_summary
        validate_summary(summary["content"], summary["ranges"], summary["documents"])
        return summary

    def read_chapters(self, ids):
        result = []
        for cid in ids:
            source = self.body(cid)
            matched = [r for r in self.chapter_reviews if r.get("source", {}).get("chapter_id") == cid
                       and r.get("decision") == "accepted" and r.get("complete_translation_context") is True]
            item = {"source": source, "translation": [], "translation_status": "unreviewed_or_missing", "review_ids": []}
            if len(matched) > 1:
                raise NarrativeError("conflict", "multiple full-chapter review decisions; choose one revision")
            if matched:
                review = matched[0]
                if review.get("id") != "chapter-review:" + digest({k: v for k, v in review.items() if k != "id"}):
                    raise NarrativeError("stale_summary", "chapter review was modified")
                self.validate_dependency(dependency(review["source"]))
                if review["source"] != source:
                    raise NarrativeError("stale_summary", "full-chapter review source changed")
                try:
                    for segment in review["translation"]:
                        body = self.body(segment["chapter_id"])
                        actual = select_segment(body, segment["paragraph_ids"], self.archive)
                        if actual != segment:
                            raise NarrativeError("stale_summary", "reviewed translation range changed")
                        item["translation"].append(actual)
                    item["review_ids"] = [review["id"]]
                    item["translation_status"] = "complete_range_coverage_semantics_unreviewed"
                except NarrativeError as exc:
                    if exc.kind not in {"visibility_denied", "quality_hold"}:
                        raise
                    item["translation"] = []
                    item["translation_status"] = exc.kind
            result.append(item)
        return result


def dependency(document):
    result = {**{k: document[k] for k in ("chapter_id", "revision_id", "paragraph_ids", "text_hash")},
              "title_hash": digest(document["title"])}
    if document.get('commit_id'):
        result.update(commit_id=document['commit_id'],branch_id=document['branch_id'],
                      canonical_source_id=document['canonical_source_id'])
    elif document.get("source") == "generated_accepted":
        result.update(acceptance_id=document["acceptance_id"], canonical_source_id=document["canonical_source_id"])
    return result


def select_segment(body, ids, archive):
    if not ids or len(set(ids)) != len(ids) or any(p not in body["paragraph_ids"] for p in ids):
        raise NarrativeError("quality_hold", "invalid translation paragraph range")
    positions = [body["paragraph_ids"].index(p) for p in ids]
    if positions != list(range(positions[0], positions[-1] + 1)):
        raise NarrativeError("quality_hold", "translation range must be contiguous")
    rows = {p["paragraph_id"]: p for p in archive.rows[body["chapter_id"]]}
    text = "\n".join(rows[p]["text"] for p in ids)
    return {**body, "paragraph_ids": ids, "paragraph_texts": [rows[p]["text"] for p in ids], "text": text, "text_hash": digest(text)}


def review_chapter_pair(view, source_id, translation_ranges, *, decision, method, reason):
    """Explicit reviewer decision, never inferred from chapter alignment scores."""
    if decision not in {"accepted", "rejected", "unresolved"} or not method or not reason:
        raise NarrativeError("invalid_record", "explicit review decision, method and reason required")
    source = view.body(source_id)
    if source["language"] != "ja":
        raise NarrativeError("invalid_record", "source must be Japanese")
    translations, used = [], set()
    chapter_coverage = {}
    for row in translation_ranges:
        body = view.body(row["chapter_id"])
        if body["language"] != "zh":
            raise NarrativeError("invalid_record", "translation must be Chinese")
        if view.archive.canonical(view.archive.chapters[row["chapter_id"]]) != source_id:
            raise NarrativeError("invalid_record", "translation range does not map to this source chapter")
        segment = select_segment(body, row["paragraph_ids"], view.archive)
        keys = {(body["chapter_id"], p) for p in segment["paragraph_ids"]}
        if used & keys:
            raise NarrativeError("invalid_record", "duplicate translation evidence")
        used.update(keys)
        chapter_coverage.setdefault(body["chapter_id"], set()).update(segment["paragraph_ids"])
        translations.append(segment)
    if decision == "accepted" and not translations:
        raise NarrativeError("quality_hold", "accepted pair needs complete translated ranges")
    if decision == "accepted":
        for cid, covered in chapter_coverage.items():
            full = set(view.body(cid)["paragraph_ids"])
            if covered != full:
                raise NarrativeError("quality_hold", "accepted full-chapter pair does not cover every translated body paragraph")
    data = {"source": source, "translation": translations, "decision": decision,
            "complete_translation_context": decision == "accepted", "whole_chapter_few_shot": False,
            "coverage_status": "all_body_paragraphs_covered" if decision == "accepted" else "not_approved",
            "semantic_status": "unreviewed", "method": method, "reason": reason,
            "policy_fingerprint": view.policy.fingerprint}
    ident = view.store.put("chapter-review", data)
    return {"id": ident, **data}


def build_block_index(view, config=None, overrides=()):
    config = config or BlockConfig()
    chapters = view.visible_sources()
    ids = [c["chapter_id"] for c in chapters]
    override_map = {o["start_id"]: o for o in overrides}
    if len(override_map) != len(overrides) or set(override_map) - set(ids):
        raise NarrativeError("visibility_denied", "boundary override is duplicate or outside visible scope")
    for o in overrides:
        if not o.get("reason") or not o.get("method") or not o.get("evidence"):
            raise NarrativeError("invalid_record", "boundary override needs reason, method and visible evidence")
        for dep in o["evidence"]:
            view.validate_dependency(dep)
    view.archive.ensure_rows(ids)
    documents = [view.body(cid) for cid in ids]
    blocks, current, tokens, reason = [], [], 0, "start_of_visible_history"

    def finish():
        if not current:
            return
        members = [d["chapter_id"] for d in current]
        bid = "block:" + digest(members)[:24]
        ranges = [{"range_id": "range:" + digest(members[i:i+config.detail_chapters])[:24],
                   "chapter_ids": members[i:i+config.detail_chapters]}
                  for i in range(0, len(members), config.detail_chapters)]
        blocks.append({"block_id": bid, "chapter_ids": members, "ranges": ranges, "input_tokens": tokens,
                       "boundary_reason": reason, "title_hint": current[0]["title"],
                       "boundary_status": "start_of_visible_history" if reason == "start_of_visible_history" else
                       "reviewed_override" if members[0] in override_map else "candidate"})

    for doc in documents:
        count = view.counter.count(f"[{doc['match_key']}] {doc['title']}\n{doc['text']}")
        if count > config.max_input_tokens:
            raise NarrativeError("budget_exceeded", "single complete chapter exceeds block budget; explicit subchapter design required")
        title = re.sub(r"^\s*[0-9０-９]+\s*", "", doc["title"])
        signal = bool(re.match(r"(?:第.+[部章編]|新章|旅立ち|新たなる|出発|帰還|再会|攻略|開戦|決着|突入|覚醒|再起)", title))
        why = None
        if current:
            if doc["order"] != current[-1]["order"] + 1:
                why = "visibility_gap"
            elif doc["chapter_id"] in override_map:
                why = "reviewed_override"
            elif len(current) >= config.max_chapters:
                why = "chapter_capacity"
            elif tokens + count > config.max_input_tokens:
                why = "token_capacity"
            elif len(current) >= config.min_chapters and signal:
                why = "title_candidate"
        if why:
            finish()
            current, tokens, reason = [], 0, why
        current.append(doc)
        tokens += count
    finish()
    data = {"version": VERSION, "policy": view.policy.public(), "config": asdict(config),
            "tokenizer": view.counter.identity, "corpus_fingerprint": view.archive.fingerprint,
            "chapter_ids": ids, "dependencies": [dependency(d) for d in documents], "blocks": blocks,
            "boundary_overrides": list(overrides), "coverage": {"indexed": len(ids), "summarized": 0},
            "policy_exclusions": [c["chapter_id"] for c in view.sources if c["chapter_id"] in view.policy.reserved_source_ids
                                  and (view.policy.cutoff_order is None or c["order"] <= view.policy.cutoff_order)]}
    index_id = view.store.put("index", data)
    return {"index_id": index_id, **data}


def review_block_boundaries(view, index_id, decisions):
    data = view.list_blocks(index_id)
    for row in decisions:
        if row.get("block_id") not in {b["block_id"] for b in data["blocks"]} or not row.get("method") or not row.get("reason"):
            raise NarrativeError("invalid_record", "boundary review requires known block/method/reason")
        if row.get("decision") not in {"accepted", "rejected", "unresolved"}:
            raise NarrativeError("invalid_record", "invalid boundary decision")
        if not row.get("evidence"):
            raise NarrativeError("invalid_record", "boundary review requires source evidence")
        for dep in row["evidence"]:
            view.validate_dependency(dep)
    stored = []
    for row in decisions:
        review = {**row, "index_id": index_id}
        review["id"] = "boundary-review:" + digest(review)
        stored.append(view.store.put("boundary-review", review))
    reviews_by_block = {r["block_id"]: r for r in data.get("boundary_reviews", [])}
    reviews_by_block.update({r["block_id"]: r for r in decisions})
    revised = {**data, "boundary_review_ids": data.get("boundary_review_ids", []) + stored,
               "boundary_reviews": [reviews_by_block[k] for k in sorted(reviews_by_block)], "parent_index": index_id}
    return {"index_id": view.store.put("index", revised), "parent_index_id": index_id,
            "boundary_review_ids": stored, "boundary_reviews": decisions, "blocks": data["blocks"],
            "chapter_ids": data["chapter_ids"], "policy": data["policy"]}
