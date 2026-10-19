"""Deterministic UTF-8 storage; atomic local commits."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable


def digest(value: Any) -> str:
    raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Hash-named artifacts already have long filenames; duplicating the name in
    # the temporary file exceeds Windows path limits in nested workspaces.
    descriptor,name=tempfile.mkstemp(prefix='.lnt-',suffix='.tmp',dir=path.parent)
    temporary=Path(name)
    try:
        with os.fdopen(descriptor,"w",encoding="utf-8",newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(8):
            try:
                temporary.replace(path)
                break
            except PermissionError as exc:
                # Concurrent NTFS rename/delete handles can briefly deny the
                # destination even though each writer owns a unique temp file.
                if os.name!='nt' or getattr(exc,'winerror',None) not in {5,32,33} or attempt==7:
                    raise
                time.sleep(min(.01*2**attempt,.1))
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, value: Any) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_jsonl(path: Path, values: Iterable[Any]) -> None:
    atomic_text(path, "".join(json.dumps(v, ensure_ascii=False, sort_keys=True) + "\n" for v in values))


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def code_fingerprint() -> str:
    root = Path(__file__).parent
    return digest({str(p.relative_to(root)): file_digest(p) for p in sorted(root.rglob("*.py"))})

