"""Private configuration, packaged skill and skill-guided recovery scenarios."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import traceback

import pytest

from lightnovel_translation import (
    ConfigurationError, TranslationConfig, TranslationError, TranslationProject,
    export_skill, init_user_config, user_config_path, user_config_status,
)
from lightnovel_translation.cli import main
from lightnovel_translation.ingestion import (
    apply_structure_patch, export_corpus, extract_sources, load_extraction,
    load_structure, propose_structure, read_source_range, validate_import,
)

_example = Path(__file__).resolve().parents[2] / "examples/library_offline.py"
_spec = importlib.util.spec_from_file_location("library_offline_user_config", _example)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)


@pytest.fixture(autouse=True)
def private_home(monkeypatch, tmp_path):
    monkeypatch.setenv("LIGHTNOVEL_TRANSLATION_HOME", str(tmp_path / "private"))
    monkeypatch.delenv("LIGHTNOVEL_TRANSLATION_API_KEY", raising=False)


def ready_file():
    init_user_config()
    path = user_config_path()
    text = path.read_text(encoding="utf-8")
    text = text.replace('model = "YOUR_MODEL"', 'model = "local-test-model"')
    text = text.replace('https://example.invalid/v1', 'http://localhost:8000/v1')
    text = text.replace('api_key = ""', 'api_key = "private-marker-do-not-echo"')
    path.write_text(text, encoding="utf-8")
    return path


def test_init_is_private_empty_non_overwriting_and_reports_missing_fields():
    result = init_user_config()
    assert result["created"] and Path(result["path"]) == user_config_path()
    status = user_config_status()
    assert status["valid"] and not status["generation_ready"] and not status["credential_present"]
    assert set(status["missing_fields"]) == {"model.model", "model.base_url", "model.api_key"}
    if os.name != "nt":
        assert stat.S_IMODE(user_config_path().stat().st_mode) == 0o600
        assert stat.S_IMODE(user_config_path().parent.stat().st_mode) == 0o700
    path = ready_file()
    before = path.read_bytes()
    assert not init_user_config()["created"]
    assert path.read_bytes() == before


def test_user_file_reused_by_project_and_secrets_stay_out_of_diagnostics(tmp_path, capsys):
    ready_file()
    config = TranslationConfig.from_user_config()
    assert config.model.api_key == "private-marker-do-not-echo"
    assert user_config_status()["generation_ready"]
    assert main(["config", "check"]) == 0
    printed = capsys.readouterr().out
    assert "private-marker-do-not-echo" not in printed + repr(config) + json.dumps(config.public())
    with TranslationProject(tmp_path / "work") as project:
        assert project.config.model.api_key == config.model.api_key
        assert not project.provider.requests_made
    assert b"private-marker-do-not-echo" not in (tmp_path / "work/project.json").read_bytes()


def test_empty_file_key_can_use_environment_and_explicit_file_key_wins(monkeypatch):
    init_user_config()
    monkeypatch.setenv("LIGHTNOVEL_TRANSLATION_API_KEY", "environment-only-marker")
    assert TranslationConfig.from_user_config().model.api_key == "environment-only-marker"
    ready_file()
    assert TranslationConfig.from_user_config().model.api_key == "private-marker-do-not-echo"


def test_missing_or_bad_config_cli_never_echoes_key_or_document(capsys):
    assert main(["config", "check"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "config_not_found"
    init_user_config()
    path = user_config_path()
    path.write_text('[model]\napi_key = "private-marker-do-not-echo"\nunknown = [', encoding="utf-8")
    assert main(["config", "check"]) == 1
    printed = capsys.readouterr().out
    assert "private-marker-do-not-echo" not in printed
    with pytest.raises(ConfigurationError) as caught:
        TranslationConfig.from_user_config()
    formatted = "".join(traceback.format_exception(caught.value))
    assert "private-marker-do-not-echo" not in formatted


def test_placeholder_endpoint_cannot_receive_requests_even_with_a_key(tmp_path):
    init_user_config()
    path = user_config_path()
    text = path.read_text(encoding="utf-8").replace('api_key = ""', 'api_key = "private-marker-do-not-echo"')
    path.write_text(text, encoding="utf-8")
    source = tmp_path / "book.txt"
    source.write_text("第1話\n\nノアは村に帰った。", encoding="utf-8")
    with TranslationProject(tmp_path / "work") as project:
        edition = project.import_book(source, edition_id="ja", language="ja")
        with project.create_job("draft", source=edition) as job:
            with pytest.raises(TranslationError) as caught:
                job.run()
            assert caught.value.code == "configuration_incomplete"
            assert not project.provider.requests_made
            assert "private-marker-do-not-echo" not in json.dumps(caught.value.to_dict())


def test_skill_export_is_complete_idempotent_and_preserves_local_changes(tmp_path):
    destination = tmp_path / "skills/lightnovel-translation"
    result = export_skill(destination)
    assert "SKILL.md" in result["files"] and "references/input-repair.md" in result["files"]
    assert (destination / "agents/openai.yaml").is_file()
    assert export_skill(destination)["files"] == result["files"]
    (destination / "SKILL.md").write_text("local customization", encoding="utf-8")
    with pytest.raises(TranslationError) as caught:
        export_skill(destination)
    assert caught.value.code == "skill_conflict"
    assert (destination / "SKILL.md").read_text(encoding="utf-8") == "local customization"


def test_skill_guided_hidden_titles_patch_export_and_translation(tmp_path):
    # Neither title matches the automatic heading grammar. Follow the actual
    # block/patch API documented by the skill, then use only the public facade.
    source = tmp_path / "hidden-titles.txt"
    source.write_text("月の朝\n\nノアは村に帰った。\n\n皆は旅の話を聞いた。\n\n帰り道\n\nノアは村に帰った。\n\n皆は旅の話を聞いた。", encoding="utf-8")
    spec = {"edition_id": "ja-repaired", "language": "ja", "edition_label": "repaired",
            "paragraph_policy": "blank_line",
            "sources": [{"source_id": "s0", "path": str(source), "input_order": 0}]}
    folder = tmp_path / "repair/extraction"
    extract_sources(spec, folder)
    base = propose_structure(folder)
    assert len(base["entries"]) == 1
    extraction = load_extraction(folder)
    lookup = {b["raw_text"]: b["block_id"] for b in extraction["blocks"]}
    headings = [lookup["月の朝"], lookup["帰り道"]]
    assert read_source_range(folder, headings, context=1)["blocks"]
    repaired = apply_structure_patch(folder, {"schema": "import-structure-patch-v1",
        "base_structure_revision": base["fingerprint"], "decision_by": "agent", "operations": [
            {"op": "set_boundary", "at": {"block_id": ident}, "title_ref": {"block_id": ident},
             "reason": "Confirmed standalone chapter heading", "evidence_refs": [ident]} for ident in headings]})
    assert len(repaired["entries"]) == 2
    assert validate_import(folder)["ok"]
    output = tmp_path / "repair/corpus"
    export_corpus(folder, output)
    provider = _module.OfflineProvider()
    with TranslationProject(tmp_path / "work", TranslationConfig(), provider=provider, provider_id="skill-repair-test-v1") as project:
        edition = project.attach_corpus(output, edition_id="ja-repaired")
        assert [c.title for c in edition.chapters()] == ["月の朝", "帰り道"]
        assert [c.paragraph_count for c in edition.chapters()] == [2, 2]
        result = project.translate(edition, name="repaired")
        assert result.state == "complete" and result.committed_count == 2, result.to_dict()


def test_encoding_ambiguity_needs_new_extraction_not_structure_patch(tmp_path):
    source = tmp_path / "legacy.txt"
    source.write_bytes(b"\xa4\xa2\n")
    spec = {"edition_id": "ambiguous", "language": "ja", "sources": [
        {"source_id": "s0", "path": str(source), "input_order": 0}]}
    old = tmp_path / "old"
    extract_sources(spec, old)
    base = propose_structure(old)
    assert not validate_import(old)["ok"]
    with pytest.raises(ValueError, match="extraction errors"):
        apply_structure_patch(old, {"schema": "import-structure-patch-v1", "base_structure_revision": base["fingerprint"],
                                  "operations": [{"op": "set_boundary"}]})
    # The fixture's declared codec is known; a real agent must obtain evidence
    # before choosing one of several successfully decoding legacy encodings.
    spec["edition_id"] = "decoded"
    spec["sources"][0]["encoding_override"] = "big5"
    new = tmp_path / "new"
    extract_sources(spec, new)
    propose_structure(new)
    assert validate_import(new)["ok"]
    assert load_structure(old)["fingerprint"] == base["fingerprint"]
