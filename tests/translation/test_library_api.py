"""Public-library workflows, with no knowledge of SequenceEngine internals."""
import importlib.util
import json
from pathlib import Path
import shutil

import pytest

from lightnovel_translation import (
    ConfigurationError, EditionIndexSpec, GenerationResult, Limits, ModelCapacity,
    ModelConfig, RoleBudget, TranslationConfig, TranslationError, TranslationProject,
)


_example = Path(__file__).resolve().parents[2] / "examples" / "library_offline.py"
_spec = importlib.util.spec_from_file_location("library_offline", _example)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
OfflineProvider = _module.OfflineProvider


@pytest.fixture(autouse=True)
def isolated_user_config(monkeypatch, tmp_path):
    # Tests must not depend on or read the developer's reusable credentials.
    monkeypatch.setenv("LIGHTNOVEL_TRANSLATION_HOME", str(tmp_path / "user-config"))


def book(tmp_path, language="ja", count=5):
    path = tmp_path / (language + ".txt")
    lines = ("ノアは村に帰った。", "皆は旅の話を聞いた。") if language == "ja" else ("诺亚回到了村子。", "众人听他讲述旅途。")
    path.write_text("\n\n".join(f"第{i}話 帰還\n\n" + "\n\n".join(lines) for i in range(1, count+1)), encoding="utf-8")
    return path


def setup(tmp_path, *, config=None, provider=None):
    provider = provider or OfflineProvider()
    config = config or TranslationConfig(index=EditionIndexSpec(child_max_chapters=2, parent_max_chapters=4))
    project = TranslationProject(tmp_path / "project", config, provider=provider, provider_id="offline-v1")
    source = project.import_book(book(tmp_path), edition_id="web", language="ja", paragraph_policy="blank_line")
    return project, source, provider


def test_local_import_plan_and_index_reads_never_generate(tmp_path):
    project, source, provider = setup(tmp_path)
    with project:
        assert len(source.chapters()) == 5
        assert source.chapters()[0].position == 0
        assert "ノア" in source.read(source.chapters()[0])["text"]
        index = project.index([source])
        assert index.plan()["local_only"]
        assert index.read(index.nodes()[0]["node_id"])["status"] == "not_built"
        with project.create_job("sample", source=source, index=index) as job:
            context = job.plan_next()["context"]
            assert context["budget"]["fits"]
            assert context["budget"]["factor"] >= 1.05
            assert "source-records-v1" in context["messages"][0]["content"]
        assert not provider.requests


def test_source_only_translate_pause_reopen_resume_compress_export(tmp_path):
    project, source, provider = setup(tmp_path)
    with project:
        events = []
        with project.create_job("source", source=source) as job:
            first = job.run(max_chapters=2, on_event=events.append)
            assert first.ok and first.state == "paused_at_limit"
            assert first.committed_count == 2
            assert [e["event"] for e in events] == ["chapter_started", "chapter_committed"] * 2
            assert job.checkpoint()
            checkpoint_messages = job.plan_next()["context"]["messages"]
        before = len(provider.requests)
        with project.open_job("source") as job:
            assert job.plan_next()["context"]["messages"] == checkpoint_messages
            assert len(provider.requests) == before
            result = job.resume(materialize=True)
            assert result.state == "complete", result.to_dict()
            assert len(job.translations()) == 5
            assert not job.status()["formal_acceptance"]
            output = job.export(tmp_path / "export", include_io=True)
            assert len(list((Path(output["artifact_dir"]) / "io").glob("*/actual-request.json"))) == 5
            before = len(provider.requests)
            assert job.resume(materialize=True).state == "complete"
            assert len(provider.requests) == before


def test_parallel_pairs_add_actual_bilingual_history_and_never_target_answers(tmp_path):
    project, source, provider = setup(tmp_path)
    with project:
        reference = project.import_book(book(tmp_path, "zh"), edition_id="zh", language="zh", paragraph_policy="blank_line")
        ja, zh = source.chapters(), reference.chapters()
        pairs = {ja[0].id: [zh[0].id]}
        with project.create_job("parallel", source=source, chapters=ja[1:3], references=[reference], parallel_pairs=pairs) as job:
            plan = job.plan_next()
            full = "\n".join(m["content"] for m in plan["context"]["messages"])
            assert "诺亚回到了村子" in full
            assert job.status()["reference_mode"] == "parallel"
            assert job.run().state == "complete"
        with pytest.raises(ValueError, match="precede"):
            project.create_job("leak", source=source, chapters=ja[1:3], references=[reference],
                               parallel_pairs={ja[1].id: [zh[1].id]})


def test_cross_edition_build_associate_translate_and_reference_boundary(tmp_path):
    project, source, provider = setup(tmp_path)
    with project:
        reference = project.import_book(book(tmp_path, "zh"), edition_id="print", language="zh", paragraph_policy="blank_line")
        index = project.index([source, reference])
        built = index.build(workers=2, include_parents=True)
        assert built["complete"], built
        parent = index.nodes(edition="web", level=1)[0]["node_id"]
        assert index.read(parent)["status"] == "found"
        assert index.entity_catalog(parent)["coverage_complete"]
        source_node, reference_node = index.nodes(edition="web")[0], index.nodes(edition="print")[0]
        association = index.associate([source_node["node_id"]], [reference_node["node_id"]])
        assert association["status"] == "complete", association
        before = len(provider.requests)
        assert index.build(workers=2, include_parents=True)["complete"]
        assert len(provider.requests) == before
        with project.create_job("cross", source=source, chapters=source.chapters()[:2], references=[reference],
                                index=index, associations=[association]) as job:
            plan = job.plan_next()
            assert plan["context"]["edition_reference"]["visibility_policy"] == "reference_assisted"
            assert "文库中文来自独立版本" in plan["context"]["messages"][0]["content"]
            assert job.status()["reference_mode"] == "cross_edition"
            result = job.run()
            assert result.state == "complete", result.to_dict()


def test_blocked_protocol_is_durable_and_does_not_become_success(tmp_path):
    class Bad(OfflineProvider):
        def generate(self, request):
            self.requests.append(request)
            return GenerationResult("bad protocol", "stop", "offline", "bad", {}, 0)
    project, source, provider = setup(tmp_path, provider=Bad())
    with project:
        with project.create_job("bad", source=source, chapters=source.chapters()[:1]) as job:
            result = job.run()
            assert result.state == "blocked" and not result.ok
            assert result.committed_count == 0 and result.error["code"] == "review_blocked"
            assert len(provider.requests) == 3
            assert Path(result.artifact_dir, "status.json").is_file()
            before = len(provider.requests)
            assert job.resume().state == "blocked"
            assert len(provider.requests) == before


def test_unconfigured_project_allows_planning_but_rejects_run(tmp_path):
    with TranslationProject(tmp_path / "local") as project:
        source = project.import_book(book(tmp_path), edition_id="ja", language="ja", paragraph_policy="blank_line")
        with project.create_job("local", source=source) as job:
            assert job.plan_next()["state"] == "ready"
            with pytest.raises(TranslationError, match="configure") as caught:
                job.run()
            assert caught.value.code == "model_not_configured"


def test_model_identity_change_rejects_resume_without_requests(tmp_path):
    project, source, provider = setup(tmp_path)
    project.create_job("frozen", source=source).close()
    project.close()
    with TranslationProject(tmp_path / "project", provider=provider, provider_id="different-model") as changed:
        with pytest.raises(TranslationError, match="configuration changed"):
            changed.open_job("frozen")
    assert not provider.requests


@pytest.mark.parametrize("name", ["../bad", "bad/dir", "CON", "x.y", "", "NUL"])
def test_invalid_workspace_ids(tmp_path, name):
    with TranslationProject(tmp_path / "safe") as project:
        with pytest.raises(ValueError):
            project.import_book(book(tmp_path), edition_id=name, language="ja")


def test_reject_skipped_chapters_foreign_handles_and_unknown_options(tmp_path):
    project, source, provider = setup(tmp_path)
    with project, TranslationProject(tmp_path / "other") as other:
        ja = source.chapters()
        with pytest.raises(ValueError, match="consecutive"):
            project.create_job("skip", source=source, chapters=[ja[0], ja[2]])
        with pytest.raises(ValueError, match="another project"):
            other.index([source])
        with pytest.raises(ConfigurationError, match="unknown"):
            TranslationConfig(sequence={"typo": 1})
        with pytest.raises(ConfigurationError, match="unknown"):
            TranslationConfig.from_dict({"capcity": {}})
    assert not provider.requests


def test_toml_json_environment_secret_redaction_and_relative_tokenizer(tmp_path, monkeypatch):
    monkeypatch.setenv("LIBRARY_TEST_API_KEY", "secret-unit-test-value")
    values = {"schema_version": 1, "model": {"model": "test", "base_url": "http://localhost:8000/v1",
               "api_key_env": "LIBRARY_TEST_API_KEY"},
              "capacity": {"context_window_tokens": 65536, "max_output_tokens": 8192},
              "roles": {"translator": {"requested_output_tokens": 4096}},
              "tokenizer_path": "local-tokenizer.json", "sequence": {"n_chapter": 2}}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    config = TranslationConfig.from_file(path)
    assert config.model.api_key == "secret-unit-test-value"
    assert config.model.thinking == "omit" and config.model.retries == 0
    assert config.tokenizer_path == tmp_path / "local-tokenizer.json"
    assert config.role_budgets()["revision"].requested_output_tokens == 4096
    assert "secret-unit-test-value" not in repr(config) + json.dumps(config.public())
    path = tmp_path / "config.toml"
    path.write_text('[model]\nmodel="test"\nbase_url="http://localhost:8000/v1"\napi_key_env="LIBRARY_TEST_API_KEY"\n', encoding="utf-8")
    assert TranslationConfig.from_file(path).model.api_key == config.model.api_key


@pytest.mark.parametrize("values", [
    {"roles": {"translator": {"requested_output_tokens": 999999}}},
    {"limits": {"max_attempts": 4}},
    {"model": {"model": "x", "base_url": "http://localhost", "retries": 2}},
    {"sequence": {"max_requests": 1}},
    {"schema_version": 2},
])
def test_invalid_config_is_rejected_before_execution(values):
    with pytest.raises(ConfigurationError):
        TranslationConfig.from_dict(values)


def test_http_model_key_is_never_written_to_artifacts_and_client_closes(tmp_path):
    config = TranslationConfig(model=ModelConfig("test", "http://localhost:8000/v1", "do-not-persist-me"))
    with TranslationProject(tmp_path / "http", config) as project:
        source = project.import_book(book(tmp_path), edition_id="ja", language="ja", paragraph_policy="blank_line")
        project.create_job("plan", source=source).close()
        provider = project.provider
    assert provider.client.is_closed
    for path in (tmp_path / "http").rglob("*"):
        if path.is_file():
            assert b"do-not-persist-me" not in path.read_bytes()


def test_easy_translate_and_context_manager_close(tmp_path):
    project, source, provider = setup(tmp_path)
    with project:
        result = project.translate(source, name="easy", chapters=source.chapters()[:1])
        assert result.state == "complete", result.to_dict()
        assert json.loads(json.dumps(result.to_dict()))["ok"]
    with pytest.raises(TranslationError, match="closed"):
        source.chapters()


def test_import_needs_resolution_does_not_register_an_edition(tmp_path):
    path = tmp_path / "ambiguous.txt"
    path.write_text("本文没有可识别的章节标题。\n正文。", encoding="utf-8")
    with TranslationProject(tmp_path / "project") as project:
        # Several legacy codecs can decode this into different valid strings.
        path.write_bytes(b"\xa4\xa2\n")
        with pytest.raises(TranslationError) as caught:
            project.import_book(path, edition_id="bad", language="ja")
        assert caught.value.code == "import_needs_resolution"
        assert not project.editions()


def test_project_can_relocate_and_resume_from_frozen_inputs(tmp_path):
    project, source, provider = setup(tmp_path)
    with project:
        project.translate(source, name="moving", chapters=source.chapters()[:2], max_chapters=1)
    moved = tmp_path / "relocated"
    shutil.copytree(tmp_path / "project", moved)
    with TranslationProject(moved, provider=provider, provider_id="offline-v1") as reopened:
        assert len(reopened.edition("web").chapters()) == 5
        with reopened.open_job("moving") as job:
            assert job.resume().state == "complete"
            assert len(job.translations()) == 2


def test_stale_attached_corpus_is_detected_and_custom_provider_stays_owned_by_caller(tmp_path):
    class Provider(OfflineProvider):
        def close(self):
            raise AssertionError("caller owns this provider")
    project, source, provider = setup(tmp_path, provider=Provider())
    with project:
        corpus = project.path / source.info["corpus_folder"]
        paragraphs = corpus / "paragraphs.jsonl"
        rows = [json.loads(line) for line in paragraphs.read_text(encoding="utf-8").splitlines()]
        rows[0]["raw_text"] = "changed"
        paragraphs.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
        with pytest.raises(ValueError, match="fingerprint"):
            source.chapters()


def test_budget_exhaustion_is_persisted_and_resume_does_not_reissue_translation(tmp_path):
    config = TranslationConfig(limits=Limits(max_requests=1))
    project, source, provider = setup(tmp_path, config=config)
    with project:
        with project.create_job("limited", source=source, chapters=source.chapters()[:1]) as job:
            result = job.run()
            assert result.state == "blocked" and result.committed_count == 0
            assert len(provider.requests) == 1
        with project.open_job("limited") as job:
            assert job.resume().state == "blocked"
            assert len(provider.requests) == 1
