"""Project facade over the existing evidence-bound translation engines."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
from typing import Any, Iterable

from translation.editions.archive import EditionArchive
from translation.ingestion import InputSpec, normalize_input
from translation.narrative.tokens import TokenCounter
from translation.providers import HTTPProvider
from translation.storage import digest, read_json, write_json

from .config import TranslationConfig
from .errors import ConfigurationError, TranslationError, operation
from .models import ChapterInfo



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

    Configuration uses the public user configuration loader. Generation is explicit.
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
        self._archive_cache = {}
        self._combined_archive_cache = {}
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
        ids = tuple(dict.fromkeys(edition_ids))
        versions = []
        for ident in ids:
            item = manifest["editions"][ident]
            folder = (self.path / item['corpus_folder']).resolve()
            stamp = tuple((name, p.stat().st_size, p.stat().st_mtime_ns, p.stat().st_ctime_ns)
                if p.is_file() else (name, None) for name in
                ('entries.jsonl', 'paragraphs.jsonl', 'manifest.json', 'provenance.jsonl', 'issues.jsonl')
                for p in [folder/name])
            version = (str(folder), item['fingerprint'], stamp)
            cached = self._archive_cache.get(ident)
            if cached is None or cached[0] != version:
                archive = EditionArchive.from_folder(folder)
                own = [e for e in archive.entries.values() if e['edition_id'] == ident]
                own_ids = {e['entry_id'] for e in own}
                rows = [r for r in archive.paragraphs.values() if r['entry_id'] in own_ids]
                archive = EditionArchive(own, rows,
                                         fingerprint=item['fingerprint'])
                if archive.fingerprint != item['fingerprint']:
                    raise TranslationError('stale_corpus', 'registered corpus changed', details={'edition_id':ident})
                self._archive_cache[ident] = (version, archive)
                self._combined_archive_cache.clear()
            versions.append(version)
        key = (ids, tuple(versions))
        if key not in self._combined_archive_cache:
            if len(ids) == 1:
                combined = self._archive_cache[ids[0]][1]
            else:
                for ident in ids:
                    archive = self._archive_cache[ident][1]
                    entries.extend(archive.entries.values())
                    paragraphs.extend(archive.paragraphs.values())
                combined = EditionArchive(
                    entries, paragraphs,
                    fingerprint=digest(tuple(self._archive_cache[i][1].fingerprint
                                             for i in ids)))
            self._combined_archive_cache[key] = combined
        return self._combined_archive_cache[key]

    @operation
    def validate_inputs(self, *, force: bool = True) -> dict[str, Any]:
        """Validate registered corpora once; force bypasses stat-based session caches."""
        if force:
            self._archive_cache.clear()
            self._combined_archive_cache.clear()
        ids = list(self._manifest()['editions'])
        archive = self._archive(ids)
        return {'valid':True, 'edition_ids':ids, 'fingerprint':archive.fingerprint,
                'validation':'full' if force else 'stat_cached'}

    def close(self) -> None:
        if self._closed:
            return
        if self._owns_provider and isinstance(self._provider, HTTPProvider):
            self._provider.client.close()
        self._closed = True

    def __enter__(self) -> TranslationProject:
        self._check_open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()
