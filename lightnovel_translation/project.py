"""Project facade over the existing evidence-bound translation engines."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
from typing import Any, Iterable, TYPE_CHECKING

from translation.editions.archive import EditionArchive
from translation.ingestion import InputSpec, normalize_input
from translation.narrative.tokens import TokenCounter
from translation.providers import HTTPProvider
from translation.storage import digest, read_json, write_json

from .config import TranslationConfig
from .errors import ConfigurationError, TranslationError, operation
from .models import ChapterInfo, RunResult

if TYPE_CHECKING:
    from .index import Index
    from .job import TranslationJob


def safe_name(value: str) -> str:
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value)
            or value.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                                *(f"LPT{i}" for i in range(1, 10))}):
        raise ValueError("IDs must be 1-80 ASCII letters/digits/underscores/hyphens and a valid filename")
    return value


class _ProviderIdentity:
    def __init__(self, identity: dict):
        self.identity = deepcopy(identity)

    def public(self) -> dict:
        return deepcopy(self.identity)


class _InjectedProvider:
    def __init__(self, provider, identity: str):
        self.provider = provider
        self.config = _ProviderIdentity({"model": identity, "adapter": "injected-provider-v1"})

    def generate(self, request):
        return self.provider.generate(request)


class _UnconfiguredProvider:
    config = _ProviderIdentity({"model": "unconfigured"})

    def generate(self, request):
        raise TranslationError("model_not_configured", "configure a model or inject a provider before generation")


class Edition:
    """A registered immutable edition. Chapter IDs are stable citation keys."""
    def __init__(self, project: TranslationProject, edition_id: str):
        self.project, self.id = project, edition_id

    def chapters(self) -> list[ChapterInfo]:
        archive = self.project._archive([self.id])
        return [ChapterInfo(d["entry_id"], self.id, i, d["edition_order"], d["raw_title"], len(d["rows"]))
                for i, d in enumerate(archive.documents(self.id))]

    def read(self, chapter: str | ChapterInfo) -> dict[str, Any]:
        archive = self.project._archive([self.id])
        ident = chapter.id if isinstance(chapter, ChapterInfo) else chapter
        if ident not in archive.entries or archive.entries[ident]["edition_id"] != self.id:
            raise ValueError("chapter does not belong to this edition")
        return archive.read(ident)

    @property
    def info(self) -> dict[str, Any]:
        return deepcopy(self.project._manifest()["editions"][self.id])


class TranslationProject:
    """A reusable local workspace, independent of repository layout and cwd.

    Configuration is never loaded from config/apikey.py. Generation is explicit.
    Each job/index has its own persistent request/token limits. One writer per job.
    """
    def __init__(self, workspace: str | Path, config: TranslationConfig | str | Path | None = None,
                 *, provider=None, provider_id: str | None = None, counter=None):
        self.path = Path(workspace).resolve()
        self.config = (TranslationConfig.from_file(config) if isinstance(config, (str, Path))
                       else TranslationConfig.load_default() if config is None else config)
        if not isinstance(self.config, TranslationConfig):
            raise TypeError("config must be TranslationConfig or a TOML/JSON filename")
        if provider is not None and (not isinstance(provider_id, str) or not provider_id.strip()):
            raise ConfigurationError("an injected provider requires a stable provider_id (model + revision)")
        self._provider = _InjectedProvider(provider, provider_id) if provider is not None else None
        self._owns_provider = provider is None
        self._closed = False
        self._jobs: list[TranslationJob] = []
        self.counter = counter or TokenCounter(self.config.tokenizer_path)
        self.path.mkdir(parents=True, exist_ok=True)
        manifest_path = self.path / "project.json"
        if not manifest_path.exists():
            write_json(manifest_path, {"schema": "novelfetch-project-v1", "editions": {}})
        self._manifest()

    def _check_open(self) -> None:
        if self._closed:
            raise TranslationError("closed", "project is closed")

    def _manifest(self) -> dict:
        self._check_open()
        result = read_json(self.path / "project.json")
        if result.get("schema") != "novelfetch-project-v1":
            raise TranslationError("unsupported_schema", "unsupported project manifest")
        return result

    @property
    def provider(self):
        self._check_open()
        if self._provider is None:
            self._provider = (HTTPProvider(self.config.model, max_requests=2**63-1)
                              if self.config.model else _UnconfiguredProvider())
        return self._provider

    def _require_generation(self) -> None:
        if isinstance(self.provider, _UnconfiguredProvider):
            raise TranslationError("model_not_configured", "configure a model or inject a provider before generation")
        if isinstance(self.provider, HTTPProvider) and self.config.generation_issues():
            raise TranslationError("configuration_incomplete", "complete the local user configuration before generation",
                                   details={"missing_fields": self.config.generation_issues()})

    def import_book(self, paths: str | Path | Iterable[str | Path], *, edition_id: str,
                    language: str, label: str | None = None, paragraph_policy: str = "auto",
                    encoding: str | None = None, version_relation: str | None = None) -> Edition:
        """Normalize EPUB/TXT files in caller-supplied reading order, without LLMs."""
        if isinstance(paths, (str, Path)):
            paths = [paths]
        spec = InputSpec.from_dict({"edition_id": safe_name(edition_id), "language": language,
            "edition_label": label or edition_id, "paragraph_policy": paragraph_policy,
            "version_relation": version_relation,
            "sources": [{"source_id": f"source-{i:04d}", "path": str(Path(p).resolve()),
                         "input_order": i, "encoding_override": encoding} for i, p in enumerate(paths)]})
        return self.import_edition(spec)

    def import_edition(self, spec: InputSpec | dict, *, base: str | Path | None = None) -> Edition:
        """Advanced normalization with per-file encoding/volume/format hints."""
        self._check_open()
        spec = InputSpec.from_dict(spec.to_dict() if isinstance(spec, InputSpec) else spec)
        safe_name(spec.edition_id)
        result = normalize_input(spec, self.path / "imports" / spec.edition_id,
                                 base=Path(base) if base is not None else None)
        if not result["ok"]:
            raise TranslationError("import_needs_resolution", "input structure needs correction before use",
                                   details=result)
        return self.attach_corpus(result["corpus_folder"], edition_id=spec.edition_id, label=spec.edition_label)

    def attach_corpus(self, folder: str | Path, *, edition_id: str, label: str | None = None) -> Edition:
        """Register a validated existing editions export, without copying or editing it."""
        safe_name(edition_id)
        folder = Path(folder).resolve()
        archive = EditionArchive.from_folder(folder)
        entries = [e for e in archive.entries.values() if e["edition_id"] == edition_id]
        if not entries or len({e["language"] for e in entries}) != 1:
            raise ValueError("edition_id must select one nonempty, single-language edition")
        own_ids = {e["entry_id"] for e in entries}
        paragraphs = [p for p in archive.paragraphs.values() if p["entry_id"] in own_ids]
        subset = EditionArchive(entries, paragraphs)
        manifest = self._manifest()
        stored_path = folder.relative_to(self.path).as_posix() if folder.is_relative_to(self.path) else str(folder)
        item = {"edition_id": edition_id, "label": label or edition_id, "language": entries[0]["language"],
                "corpus_folder": stored_path, "fingerprint": subset.fingerprint}
        previous = manifest["editions"].get(edition_id)
        if previous and previous != item:
            raise TranslationError("edition_conflict", "edition ID is bound to another input; use a new ID")
        manifest["editions"][edition_id] = item
        write_json(self.path / "project.json", manifest)
        return Edition(self, edition_id)

    def editions(self) -> list[Edition]:
        return [Edition(self, ident) for ident in self._manifest()["editions"]]

    def edition(self, edition_id: str) -> Edition:
        if edition_id not in self._manifest()["editions"]:
            raise KeyError(edition_id)
        return Edition(self, edition_id)

    def _edition_id(self, edition: str | Edition) -> str:
        if isinstance(edition, Edition):
            if edition.project is not self:
                raise ValueError("edition handle belongs to another project")
            return edition.id
        return self.edition(edition).id

    def _archive(self, edition_ids: Iterable[str]) -> EditionArchive:
        manifest, entries, paragraphs = self._manifest(), [], []
        for ident in dict.fromkeys(edition_ids):
            item = manifest["editions"][ident]
            archive = EditionArchive.from_folder(self.path / item["corpus_folder"])
            own = [e for e in archive.entries.values() if e["edition_id"] == ident]
            ids = {e["entry_id"] for e in own}
            rows = [r for r in archive.paragraphs.values() if r["entry_id"] in ids]
            if EditionArchive(own, rows).fingerprint != item["fingerprint"]:
                raise TranslationError("stale_corpus", "registered corpus changed", details={"edition_id": ident})
            entries.extend(own)
            paragraphs.extend(rows)
        return EditionArchive(entries, paragraphs)

    @operation
    def index(self, editions: Iterable[str | Edition] | None = None) -> Index:
        """Plan an index locally. Call Index.build() separately to pay for generation."""
        from .index import Index
        ids = [self._edition_id(e) for e in editions] if editions is not None else list(self._manifest()["editions"])
        if not ids or len(set(ids)) != len(ids):
            raise ValueError("index needs unique edition IDs")
        return Index(self, ids)

    @operation
    def create_job(self, name: str, *, source: str | Edition,
                   chapters: Iterable[str | ChapterInfo] | None = None, mode: str = "auto",
                   references: Iterable[str | Edition] = (), index: Index | None = None,
                   parallel_pairs: dict[str, list[str]] | None = None,
                   associations: Iterable[dict] = (), entity_matches: Iterable[dict] = (),
                   resolve_terminology: bool = False, packet_token_budget: int = 24000) -> TranslationJob:
        """Freeze a consecutive translation window; only terminology resolution may call LLMs."""
        from .job import create_job
        return create_job(self, safe_name(name), source=source, chapters=chapters, mode=mode,
                          references=references, index=index, parallel_pairs=parallel_pairs,
                          associations=associations, entity_matches=entity_matches,
                          resolve_terminology=resolve_terminology, packet_token_budget=packet_token_budget)

    @operation
    def open_job(self, name: str) -> TranslationJob:
        """Open frozen job inputs; reopening never rebuilds references or indexes."""
        from .job import TranslationJob
        return TranslationJob(self, safe_name(name))

    def translate(self, source: str | Edition, *, name: str, max_chapters: int | None = None,
                  **options) -> RunResult:
        """Create/reuse a job, run it and export committed results."""
        with self.create_job(name, source=source, **options) as job:
            return job.run(max_chapters=max_chapters)

    def close(self) -> None:
        if self._closed:
            return
        for job in tuple(self._jobs):
            job.close()
        if self._owns_provider and isinstance(self._provider, HTTPProvider):
            self._provider.client.close()
        self._closed = True

    def __enter__(self) -> TranslationProject:
        self._check_open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()
