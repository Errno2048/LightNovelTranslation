"""Locate the same skill assets in an installed wheel or an editable checkout."""
from importlib.resources import files
from pathlib import Path


def skill_resources():
    bundled = files("lightnovel_translation").joinpath("resources/skills/lightnovel-translation")
    if bundled.joinpath("SKILL.md").is_file():
        return bundled
    # In a src-layout editable install, the canonical skill is outside the package.
    checkout = Path(__file__).resolve().parents[2]
    source = checkout / "skills" / "lightnovel-translation"
    if (checkout / "pyproject.toml").is_file() and (source / "SKILL.md").is_file():
        return source
    raise FileNotFoundError("Bundled lightnovel-translation skill is missing; reinstall the package.")
