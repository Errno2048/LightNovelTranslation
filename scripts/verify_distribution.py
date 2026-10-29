"""Install a wheel into a fresh venv and exercise it outside the repository.

No model requests or local credential files are used. Dependency installation
uses pip's configured index. The report retains paths to the isolated artifacts.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import venv
import zipfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    wheel = args.wheel.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        skill = root / "skills" / "lightnovel-translation"
        skill_prefix = "lightnovel_translation/resources/skills/lightnovel-translation/"
        expected_skill = {p.relative_to(skill).as_posix(): p.read_bytes()
                          for p in skill.rglob("*") if p.is_file()}
        bundled_skill = {name.removeprefix(skill_prefix): archive.read(name)
                         for name in names if name.startswith(skill_prefix) and not name.endswith("/")}
        assert bundled_skill == expected_skill, "Wheel skill differs from the canonical skills/ directory"
        for skill_folder in (root / "skills").iterdir():
            if not skill_folder.is_dir():
                continue
            prefix = "lightnovel_translation/resources/skills/" + skill_folder.name + "/"
            expected = {p.relative_to(skill_folder).as_posix(): p.read_bytes()
                        for p in skill_folder.rglob("*") if p.is_file()}
            actual = {name.removeprefix(prefix): archive.read(name)
                      for name in names if name.startswith(prefix) and not name.endswith("/")}
            assert actual == expected, "Wheel skill differs: " + skill_folder.name
        assert "lightnovel_translation/py.typed" in names
        assert "lightnovel_translation/resources/skills/lightnovel-translation/assets/config.example.toml" in names
        assert "lightnovel_translation/resources/skills/lightnovel-translation/SKILL.md" in names
        assert "lightnovel_translation/resources/skills/lightnovel-translation/references/workflows.md" in names
        assert "lightnovel_translation/resources/skills/lightnovel-translation/references/diagnostics.md" in names
        assert "translation/editions/retrieval.py" in names
        assert "translation/ingestion/service.py" in names
        assert not any("apikey" in name or name.startswith(("config/", "out_", "artifacts/", "workspaces/", "tests/")) for name in names)
    environment = output / "venv"
    venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    external = Path(tempfile.mkdtemp(prefix="lightnovel-translation-"))
    shutil.copy2(root / "examples/library_offline.py", external / "demo.py")

    def call(label, command, *, expected_returncode=0):
        result = subprocess.run(command, cwd=external, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", env={**os.environ, "PYTHONUTF8": "1"})
        (output / (label + ".log")).write_text(result.stdout + result.stderr, encoding="utf-8")
        if result.returncode != expected_returncode:
            raise RuntimeError(label + " failed; see " + str(output / (label + ".log")))
        return result.stdout

    call("install", [str(python), "-m", "pip", "install", "--force-reinstall", str(wheel)])
    inspected = json.loads(call("inspect", [str(python), "-I", "-c",
        "import importlib.metadata as m, json, pathlib, lightnovel_translation as n; "
        "from lightnovel_translation import ingestion; import translation; "
        "print(json.dumps({'version':m.version('lightnovel-translation'), 'module':n.__file__, "
        "'typed':pathlib.Path(n.__file__).with_name('py.typed').is_file(), "
        "'reference_config':hasattr(n,'ReferenceConfig'), 'engine':translation.__file__}))"]))
    assert not Path(inspected["module"]).resolve().is_relative_to(root)
    assert not Path(inspected["engine"]).resolve().is_relative_to(root)
    assert inspected["version"] == "0.4.0" and inspected["typed"] and inspected['reference_config']
    demo = json.loads(call("demo", [str(python), "-I", str(external / "demo.py"), str(external / "run")]))
    assert demo["planned_blocks"] > 0
    assert demo["fits"] is True
    assert demo["requests"] == 2
    assert demo["text"] == "诺亚回到了村子。\n众人听他讲述旅途。"
    assert demo["context_segments"][0][0] == "summary"
    cli = environment / ("Scripts/novel-translate.exe" if os.name == "nt" else "bin/novel-translate")
    help_text = call("cli", [str(cli), "--help"])
    assert "import" in help_text
    public_cli = environment / ("Scripts/lightnovel-translate.exe" if os.name == "nt" else "bin/lightnovel-translate")
    initialized = json.loads(call("config-init", [str(public_cli), "config", "init", "--directory", str(external / "private")]))
    assert initialized["created"]
    config_status = json.loads(call("config-check", [str(public_cli), "config", "check", "--directory", str(external / "private")], expected_returncode=1))
    assert config_status["valid"] and not config_status["generation_ready"]
    exported_skill = json.loads(call("skill", [str(public_cli), "skill", "export", str(external / "skill")]))
    assert Path(exported_skill["skill_dir"], "references/input-repair.md").is_file()
    assert {p.relative_to(external / "skill").as_posix(): p.read_bytes()
            for p in (external / "skill").rglob("*") if p.is_file()} == expected_skill
    assert (external / "private/config.toml").read_bytes() == expected_skill["assets/config.example.toml"].replace(b"\r\n", b"\n")
    acquisition_skill = json.loads(call("source-skill", [str(public_cli), "skill", "export",
        str(external / "syosetu-fetch"), "--name", "syosetu-fetch"]))
    assert Path(acquisition_skill["skill_dir"], "references/acquisition.md").is_file()
    assert "inspect-reference" in call("source-cli", [str(public_cli), "syosetu", "--help"])
    source_preview = external / "reference-head.txt"
    source_preview.write_text("作者：某作者\n\n第一章\n\n参考内容。", encoding="utf-8")
    preview = json.loads(call("source-preview", [str(public_cli), "syosetu", "inspect-reference", str(source_preview)]))
    assert preview["author_candidates"] == ["某作者"]
    report = {"ok": True, "wheel": str(wheel), "installed": inspected, "external_cwd": str(external),
              "demo": demo, "cli_help_ok": True, "user_config_ok": True, "skill_export_ok": True,
              "acquisition_skill_export_ok": True, "source_preview_ok": True,
              "model_requests": 0}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
