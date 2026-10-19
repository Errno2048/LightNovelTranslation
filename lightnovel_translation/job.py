"""Durable translation jobs with audit, incremental knowledge and compression."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

from translation.editions.sequence import build_sequence_inputs, _source_document
from translation.narrative.schemas import NarrativeError
from translation.providers import ProviderError
from translation.sequence import SequenceEngine, SequenceRepository, create_sequence
from translation.storage import atomic_text, digest, read_json, write_json, write_jsonl

from .errors import TranslationError, operation
from .models import ChapterInfo, RunResult

if TYPE_CHECKING:
    from .project import TranslationProject


def create_job(project, name, *, source, chapters, mode, references, index, parallel_pairs,
               associations, entity_matches, resolve_terminology, packet_token_budget):
    source_id = project._edition_id(source)
    refs = [project._edition_id(e) for e in references]
    if source_id in refs or len(set(refs)) != len(refs):
        raise ValueError("reference editions must be unique and different from the source")
    if mode not in {"auto", "source_only", "parallel", "cross_edition"}:
        raise ValueError("unsupported reference mode")
    pairs = parallel_pairs or {}
    associations, entity_matches = list(associations), list(entity_matches)
    if mode == "auto":
        mode = "parallel" if pairs else "cross_edition" if refs else "source_only"
    if mode == "source_only" and (refs or pairs or associations or entity_matches or resolve_terminology):
        raise ValueError("source_only cannot accept reference materials")
    if mode == "parallel" and (not pairs or not refs or associations or entity_matches or resolve_terminology):
        raise ValueError("parallel requires reference editions and explicit historical pairs only")
    if mode == "cross_edition" and (not refs or pairs):
        raise ValueError("cross_edition requires independent reference editions, without parallel pairs")
    if type(packet_token_budget) is not int or packet_token_budget <= 0:
        raise ValueError("packet_token_budget must be positive")
    if index is None:
        index = project.index([source_id, *refs])
    if index.project is not project or {source_id, *refs} != set(index.edition_ids):
        raise ValueError("index must contain exactly this project's source and declared reference editions")
    index._check()
    archive = index.archive
    docs = archive.documents(source_id)
    if not docs or any(d["language"] != "ja" for d in docs):
        raise ValueError("translation source must be a Japanese narrative edition")
    if any(not archive.documents(r) or any(d["language"] != "zh" for d in archive.documents(r)) for r in refs):
        raise ValueError("reference editions must be Chinese")
    selected = [c.id if isinstance(c, ChapterInfo) else c for c in chapters] if chapters is not None else [d["entry_id"] for d in docs]
    available = [d["entry_id"] for d in docs]
    if (not selected or len(set(selected)) != len(selected) or any(c not in available for c in selected)):
        raise ValueError("targets must be unique chapters of the source edition")
    start = available.index(selected[0])
    if available[start:start+len(selected)] != selected:
        raise ValueError("targets must form a consecutive reading window in source order")
    if resolve_terminology:
        project._require_generation()
    inputs = build_sequence_inputs(index._pipeline, selected, associations=associations,
        entity_matches=entity_matches, resolve_terminology=resolve_terminology,
        packet_token_budget=packet_token_budget)
    if mode != "cross_edition":
        inputs.pop("edition_reference_packets", None)
        inputs["reference_mode"] = mode
    if mode == "parallel":
        history, used = [], set()
        reference_ids = {d["entry_id"] for r in refs for d in archive.documents(r)}
        cutoff = docs[start]["edition_order"]
        for sid, translations in pairs.items():
            if sid not in available or archive.entries[sid]["edition_order"] >= cutoff:
                raise ValueError("parallel history must strictly precede every target chapter")
            if isinstance(translations, (str, bytes)) or not translations:
                raise ValueError("each parallel value must be a nonempty list of translation chapter IDs")
            src = _source_document(archive, sid)
            target_docs = []
            for tid in translations:
                entry = archive.entries.get(tid)
                if not entry or tid not in reference_ids or entry["language"] != "zh" or tid in used:
                    raise ValueError("translation is absent, reused, or outside the declared reference editions")
                used.add(tid)
                translated = _source_document(archive, tid)
                translated.update(order=src["order"], match_key=src["match_key"], canonical_source_id=sid)
                target_docs.append(translated)
            history.append({"source": src, "translation": target_docs, "semantic_status": "caller_asserted_parallel"})
        inputs["history_pairs"] = sorted(history, key=lambda p: p["source"]["order"])
        represented = {p["source"]["chapter_id"] for p in history}
        for gap in inputs.get("explicit_gaps", []):
            gap["source_ids"] = [sid for sid in gap["source_ids"] if sid not in represented]
    folder = project.path / "jobs" / name
    folder.mkdir(parents=True, exist_ok=True)
    spec = project.config.sequence_spec()
    repo = create_sequence(folder / "sequence.sqlite", spec, inputs, branch_name=name,
        provider_identity=project.provider.config.public(), counter_identity=project.counter.identity)
    try:
        metadata = {"schema": "novelfetch-job-v1", "name": name, "run_id": repo.run_id,
                    "reference_mode": mode, "index_id": index.id, "source_edition": source_id,
                    "reference_editions": refs, "frozen_input_hash": digest(inputs)}
        metadata_path = folder / "job.json"
        if metadata_path.exists() and read_json(metadata_path) != metadata:
            raise TranslationError("job_conflict", "job name is bound to another frozen input")
        write_json(metadata_path, metadata)
        write_json(folder / "config.redacted.json", project.config.public())
    finally:
        repo.close()
    return TranslationJob(project, name)


class TranslationJob:
    """One frozen, resumable sequence. Working commits are not human acceptance."""
    def __init__(self, project: TranslationProject, name: str):
        project._check_open()
        self.project, self.name = project, name
        self.path = project.path / "jobs" / name
        if not (self.path / "job.json").is_file() or not (self.path / "sequence.sqlite").is_file():
            raise FileNotFoundError("translation job not found: " + name)
        self.repo = SequenceRepository(self.path / "sequence.sqlite")
        self._closed = False
        try:
            metadata = read_json(self.path / "job.json")
            if metadata.get("schema") != "novelfetch-job-v1" or metadata["run_id"] != self.repo.run_id:
                raise TranslationError("job_conflict", "job metadata differs from the frozen sequence")
            self.engine = SequenceEngine(self.repo, project.provider, project.counter)
        except BaseException:
            self.repo.close()
            raise
        project._jobs.append(self)

    def _check(self) -> None:
        self.project._check_open()
        if self._closed:
            raise TranslationError("closed", "translation job is closed")

    def status(self) -> dict[str, Any]:
        self._check()
        chapters = self.repo.chapters()
        return {"schema": "novelfetch-job-status-v1", "job_name": self.name, "run_id": self.repo.run_id,
                "complete": all(c["state"] == "committed" for c in chapters), "chapters": chapters,
                "committed_count": len(self.repo.commits()), "target_count": len(chapters),
                "heads": self.repo.heads(), "attempt_count": len(self.repo.store.attempts()),
                "reference_mode": self.repo.manifest["inputs"]["reference_mode"],
                "formal_acceptance": False, "artifact_dir": str(self.path)}

    @operation
    def plan_next(self) -> dict[str, Any]:
        """Inspect exact messages and budget; local checkpoint metadata may be saved."""
        self._check()
        return self.engine.plan_next()

    def run(self, *, max_chapters: int | None = None, materialize: bool = False,
            on_event: Callable[[dict[str, Any]], None] | None = None) -> RunResult:
        """Translate/review/commit in order. A blocked step preserves all paid work."""
        self._check()
        if max_chapters is not None and (type(max_chapters) is not int or max_chapters <= 0):
            raise ValueError("max_chapters must be a positive integer")
        if not self.status()["complete"]:
            self.project._require_generation()
        completed, error = [], None
        state = "complete" if self.status()["complete"] else "paused_at_limit"
        while state != "complete" and (max_chapters is None or len(completed) < max_chapters):
            if on_event:
                on_event({"event": "chapter_started", "job_name": self.name,
                          "source_id": self.engine.next_chapter()["source_id"]})
            try:
                result = self.engine.advance_sequence(stop_after=1)
            except (NarrativeError, ProviderError) as exc:
                result = {"state": "blocked", "error": exc.kind, "message": str(exc)}
            completed.extend(result.get("completed", []))
            state = result["state"]
            if state == "blocked":
                error = {"code": result.get("error"), "message": result.get("message"),
                         "source_id": result.get("source_id")}
            if on_event:
                on_event({"event": "chapter_blocked" if state == "blocked" else "chapter_committed",
                          "job_name": self.name, "result": deepcopy(result)})
            if state == "blocked":
                break
        if materialize and self.repo.commits():
            self.project._require_generation()
            try:
                nodes = self.engine.materialize_pending(seal_tail=True)
                if any(n["state"] not in {"extracted", "partial"} for n in nodes):
                    state, error = "blocked", {"code": "incomplete_incremental_index", "phase": "materialization"}
            except (NarrativeError, ProviderError) as exc:
                state, error = "blocked", {"code": exc.kind, "message": str(exc), "phase": "materialization"}
        exported = self.export()
        result = RunResult(state, self.name, self.repo.run_id, len(self.repo.commits()), tuple(completed),
                           exported["artifact_dir"], error)
        write_json(self.path / "last-result.json", result.to_dict())
        return result

    def resume(self, **options) -> RunResult:
        return self.run(**options)

    @operation
    def checkpoint(self) -> str | None:
        """Explicitly compress committed history; this operation may call the model."""
        self._check()
        if self.engine.next_chapter() is None:
            return None
        self.project._require_generation()
        return self.engine.checkpoint(force=True)

    def translations(self) -> list[dict[str, Any]]:
        """Read committed results only, including their quality and evidence labels."""
        self._check()
        rows = []
        for ident, commit in self.repo.commits():
            self.repo.validate_commit(ident)
            source = next(d for d in self.repo.manifest["inputs"]["targets"] if d["chapter_id"] == commit["source_id"])
            rows.append({"source_id": commit["source_id"], "title": source["title"], "commit_id": ident,
                         "text": commit["text"], "quality_labels": commit["quality_labels"],
                         "unresolved": commit["unresolved"]})
        return rows

    @operation
    def explain(self, chapter: str | ChapterInfo, output: str | Path | None = None) -> dict[str, Any]:
        """Export full input/output/review artifacts, with no model calls."""
        self._check()
        ident = chapter.id if isinstance(chapter, ChapterInfo) else chapter
        if ident not in {c["source_id"] for c in self.repo.chapters()}:
            raise ValueError("chapter is not a target of this job")
        destination = Path(output).resolve() if output is not None else self.path / "io" / digest(ident)[:20]
        destination.mkdir(parents=True, exist_ok=True)
        return self.engine.explain_context(ident, destination)

    def export(self, output: str | Path | None = None, *, include_io: bool = False) -> dict[str, Any]:
        """Export UTF-8 text + JSONL and optional per-chapter exact prompts/responses."""
        self._check()
        destination = Path(output).resolve() if output is not None else self.path / "export"
        rows = self.translations()
        write_jsonl(destination / "chapters.jsonl", rows)
        atomic_text(destination / "translation.txt", "\n\n".join(r["title"] + "\n\n" + r["text"] for r in rows))
        write_json(destination / "status.json", self.status())
        if include_io:
            for c in self.repo.chapters():
                if c.get("context_id") or c["state"] == "committed":
                    self.explain(c["source_id"], destination / "io" / digest(c["source_id"])[:20])
        return {"artifact_dir": str(destination), "committed_count": len(rows), "include_io": include_io}

    def close(self) -> None:
        if not self._closed:
            self.repo.close()
            self._closed = True
            if self in self.project._jobs:
                self.project._jobs.remove(self)

    def __enter__(self) -> TranslationJob:
        self._check()
        return self

    def __exit__(self, *exc) -> None:
        self.close()
