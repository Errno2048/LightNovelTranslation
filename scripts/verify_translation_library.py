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
        assert "lightnovel_translation/py.typed" in names
        assert "lightnovel_translation/resources/config.example.toml" in names
        assert "lightnovel_translation/resources/skills/lightnovel-translation/SKILL.md" in names
        assert "translation/ingestion/service.py" in names
        assert not any(name.startswith("novelfetch_translation/") for name in names)
        assert not any("apikey" in name or name.startswith(("config/", "out_", "artifacts/")) for name in names)
    environment = output / "venv"
    venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    external = Path(tempfile.mkdtemp(prefix="novelfetch-library-"))
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
        "from lightnovel_translation import ingestion; "
        "print(json.dumps({'version':m.version('lightnovel-translation'), 'module':n.__file__, "
        "'typed':pathlib.Path(n.__file__).with_name('py.typed').is_file()}))"]))
    assert not Path(inspected["module"]).resolve().is_relative_to(root / "lightnovel_translation")
    assert inspected["version"] == "0.4.0" and inspected["typed"]
    demo = json.loads(call("demo", [str(python), "-I", str(external / "demo.py"), str(external / "run")]))
    assert demo["first"]["state"] == "paused_at_limit"
    assert demo["final"]["state"] == "complete" and demo["final"]["committed_count"] == 5
    assert demo["checkpoint"]
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
    report = {"ok": True, "wheel": str(wheel), "installed": inspected, "external_cwd": str(external),
              "demo": demo, "cli_help_ok": True, "user_config_ok": True, "skill_export_ok": True,
              "model_requests": 0}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
