"""Export the bundled agent skill without overwriting local customization."""
from __future__ import annotations

from ._resources import skill_resources
from pathlib import Path

from .errors import TranslationError


def export_skill(destination: str | Path) -> dict:
    destination = Path(destination).expanduser().resolve()
    root = skill_resources()
    resources = []

    def collect(folder, prefix=Path()):
        for child in folder.iterdir():
            relative = prefix / child.name
            if child.is_dir():
                collect(child, relative)
            else:
                resources.append((relative, child.read_bytes()))
    collect(root)
    # Preflight the complete set so a conflict never partially updates the skill.
    for relative, content in resources:
        target = destination / relative
        if target.exists() and (not target.is_file() or target.read_bytes() != content):
            raise TranslationError("skill_conflict", "skill file has local changes; choose a new destination",
                                   details={"path": str(target)})
    for relative, content in resources:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            with target.open("xb") as stream:
                stream.write(content)
    return {"skill_dir": str(destination), "files": [p.as_posix() for p, _ in resources]}
