"""Locate the same skill assets in an installed wheel or an editable checkout."""
from importlib.resources import files
from pathlib import Path


SKILL_NAMES = ("lightnovel-translation", "syosetu-fetch")


def skill_resources(name="lightnovel-translation"):
    if name not in SKILL_NAMES:
        raise ValueError("Unknown bundled skill")
    bundled = files("lightnovel_translation").joinpath("resources/skills/" + name)
    if bundled.joinpath("SKILL.md").is_file():
        return bundled
    # In a src-layout editable install, the canonical skill is outside the package.
    checkout = Path(__file__).resolve().parents[2]
    source = checkout / "skills" / name
    if (checkout / "pyproject.toml").is_file() and (source / "SKILL.md").is_file():
        return source
    raise FileNotFoundError("Bundled skill is missing; reinstall the package.")
