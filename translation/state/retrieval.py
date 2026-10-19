"""Policy-scoped exact-text search over chapter paragraphs; no generation calls."""
from __future__ import annotations

from ..storage import digest
from .schemas import P6Error


class EvidenceSearch:
    def __init__(self, view, *, max_calls=4):
        self.view = view
        self._index = {}
        self._by_id = {}
        self.calls = 0
        self.max_calls = max_calls
        self.gaps = {}

    def _paragraphs(self, languages, include_target):
        key = (tuple(sorted(languages)), bool(include_target))
        if key in self._index:
            return self._index[key]
        view = self.view
        docs = []
        gaps = []
        candidates = list(view.narrative.visible_sources())
        policy = view.narrative.policy
        target = view.narrative.archive.chapters.get(policy.target_id) if include_target and policy.target_id else None
        if target and target not in candidates:
            candidates.append(target)
        source_ids = [c["chapter_id"] for c in candidates]
        reviews = {}
        translation_ids = []
        if "zh" in languages:
            for review in view.narrative.chapter_reviews:
                cid = review.get("source", {}).get("chapter_id")
                if (cid in source_ids and review.get("decision") == "accepted" and
                        review.get("complete_translation_context") is True):
                    reviews.setdefault(cid, []).append(review)
                    translation_ids.extend(s.get("chapter_id") for s in review.get("translation", []))
        # One corpus scan for all allowed source/reference paragraphs. Calling
        # body() before this batch would otherwise rescan the JSONL once/chapter.
        view.narrative.archive.ensure_rows(source_ids + translation_ids)
        for src in candidates:
            try:
                view.narrative.check(src, task=src["chapter_id"] == policy.target_id)
                doc = view.narrative.body(src["chapter_id"], task=src["chapter_id"] == policy.target_id)
            except Exception as exc:
                gaps.append({'source_id':src['chapter_id'], 'language':'ja', 'reason':type(exc).__name__})
                continue
            if "ja" in languages:
                for i, (pid, txt) in enumerate(zip(doc["paragraph_ids"], doc["paragraph_texts"])):
                    docs.append({"language":"ja", "chapter_id":src["chapter_id"], "source_id":src["chapter_id"],
                        "revision_id":doc["revision_id"], "order":src["order"], "title":src["title"],
                        "paragraph_id":pid, "ordinal":i, "text":txt})
            if "zh" in languages and src["chapter_id"] != policy.target_id:
                try:
                    if hasattr(view.narrative,'repo'):
                        translations=view.narrative.read_chapters([src['chapter_id']])[0]['translation']
                        if not translations:
                            gaps.append({'source_id':src['chapter_id'],'language':'zh','reason':'no_branch_or_reference_pair'})
                        for trans in translations:
                            for i,(pid,txt) in enumerate(zip(trans['paragraph_ids'],trans['paragraph_texts'])):
                                docs.append({'language':'zh','chapter_id':trans['chapter_id'],'source_id':src['chapter_id'],
                                    'revision_id':trans['revision_id'],'order':src['order'],'title':trans['title'],
                                    'paragraph_id':pid,'ordinal':i,'text':txt})
                        continue
                    matched = reviews.get(src["chapter_id"], [])
                    if len(matched) != 1:
                        gaps.append({'source_id':src['chapter_id'],'language':'zh','reason':'no_unique_complete_pair'})
                        continue
                    review = matched[0]
                    if review.get("id") != "chapter-review:" + digest({k: v for k, v in review.items() if k != "id"}):
                        gaps.append({'source_id':src['chapter_id'],'language':'zh','reason':'invalid_pair_hash'})
                        continue
                    if review.get("source") != doc:
                        gaps.append({'source_id':src['chapter_id'],'language':'zh','reason':'stale_source_pair'})
                        continue
                    translations = []
                    for segment in review["translation"]:
                        body = view.narrative.body(segment["chapter_id"])
                        from ..narrative.service import select_segment
                        actual = select_segment(body, segment["paragraph_ids"], view.narrative.archive)
                        if actual != segment:
                            raise P6Error("stale_evidence", "reviewed translation changed")
                        translations.append(actual)
                except Exception as exc:
                    gaps.append({'source_id':src['chapter_id'],'language':'zh','reason':type(exc).__name__})
                    continue
                for trans in translations:
                    for i, (pid, txt) in enumerate(zip(trans["paragraph_ids"], trans["paragraph_texts"])):
                        docs.append({"language":"zh", "chapter_id":trans["chapter_id"], "source_id":src["chapter_id"],
                            "revision_id":trans["revision_id"], "order":src["order"], "title":trans["title"],
                            "paragraph_id":pid, "ordinal":i, "text":txt})
        self._index[key] = docs
        self.gaps[key] = gaps
        return docs

    def search(self, query: str, *, languages=("ja",), purpose="historical_usage", limit=8, include_target=False, order_range=None):
        if not isinstance(query, str) or not query.strip() or not set(languages) <= {"ja", "zh"}:
            raise P6Error("invalid_query", "nonempty query and ja/zh languages required")
        if type(limit) is not int or not 1 <= limit <= 32:
            raise P6Error("budget_exceeded", "search limit must be 1..32")
        self.calls += 1
        if self.calls > self.max_calls:
            raise P6Error("budget_exceeded", "maximum four distinct searches per view")
        rows = self._paragraphs(languages, include_target)
        terms = [query.strip()]
        # Character fallback is intentionally a ranking aid, never an evidence substitute.
        compact = "".join(query.split())
        if len(compact) >= 2:
            terms.extend(compact[i:i+2] for i in range(len(compact)-1))
        hits = []
        seen = set()
        for row in rows:
            if order_range and not order_range[0] <= row['order'] <= order_range[1]:
                continue
            text = row["text"]
            normalized = "".join(text.split())
            exact = query in text
            score = 100 if exact else sum(normalized.count(t) for t in terms[1:])
            if not exact and not score:
                continue
            eid = "p6-evidence:" + digest({k: row[k] for k in ("chapter_id","revision_id","paragraph_id","language")})[:24]
            if eid in seen:
                continue
            evidence = {**row, "id":eid, "purpose":purpose, "text_hash":digest(text),
                        "source_dependencies":[row["source_id"]], "match":"exact" if exact else "character_fallback",
                        "score":score, "corpus_fingerprint":self.view.narrative.archive.fingerprint}
            hits.append(evidence); seen.add(eid)
        hits.sort(key=lambda x:(x["match"] != "exact", -x["score"], x["order"], x["ordinal"]))
        self._by_id.update({hit['id']:hit for hit in hits[:limit]})
        gaps = self.gaps[(tuple(sorted(languages)),bool(include_target))]
        return {"status":"hits" if hits else "not_found_in_covered_text", "coverage":"partial" if gaps else "scanned_policy_visible_paragraphs",
                "coverage_gaps":gaps,
                "query":query, "hits":hits[:limit], "view_fingerprint":self.view.view_fingerprint,
                "generation_calls":0}

    def read(self, evidence_id: str, *, surrounding=0):
        if evidence_id not in self._by_id:
            raise P6Error("not_covered", "evidence ID was not returned by this view")
        if type(surrounding) is not int or not 0 <= surrounding <= 2:
            raise P6Error("budget_exceeded", "neighbor range must be 0..2")
        hit = self._by_id[evidence_id]
        src = hit["source_id"]
        chapter = self.view.narrative.archive.chapters[src]
        if src == self.view.narrative.policy.target_id:
            self.view.narrative.check(chapter, task=True)
        else:
            self.view.narrative.check(chapter)
        return {**hit, "neighbors": self._neighbors(hit, surrounding), "generation_calls":0}

    def _neighbors(self, hit, radius):
        if not radius:
            return []
        rows = self.view.narrative.archive.rows.get(hit["chapter_id"], [])
        if not rows:
            self.view.narrative.archive.ensure_rows([hit["chapter_id"]]); rows = self.view.narrative.archive.rows[hit["chapter_id"]]
        pos = next((i for i,r in enumerate(rows) if r["paragraph_id"] == hit["paragraph_id"]), None)
        if pos is None:
            raise P6Error("stale_evidence", "paragraph no longer exists")
        return [{"paragraph_id":r["paragraph_id"],"ordinal":r["ordinal"],"text":r["text"]}
                for r in rows[max(0,pos-radius):pos+radius+1] if r.get("kind") == "body"]
