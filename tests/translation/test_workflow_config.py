from lightnovel_translation import TranslationConfig


def test_workflow_defaults_are_config_driven():
    config = TranslationConfig.from_dict({})
    workflow = config.workflow
    assert workflow.translation_concurrency == 3
    assert workflow.review_concurrency == 4
    assert workflow.batch_size == 30
    assert workflow.full_chapters == 5
    assert workflow.history_chapters == 3
    assert workflow.summary_every == 5
    assert workflow.summary_lag == 0
    assert workflow.reference_chunk == 16
    assert workflow.max_revisions == 2
    assert workflow.translation_tokens == 65536
    assert config.public()["workflow"]["batch_size"] == 30


def test_legacy_sequence_concurrency_is_migrated():
    config = TranslationConfig.from_dict({
        "sequence": {
            "translation_concurrency": 5,
            "review_concurrency": 6,
            "max_revisions": 1,
            "output_profile": "source-records-v1",
        }
    })
    assert config.workflow.translation_concurrency == 5
    assert config.workflow.review_concurrency == 6
    assert config.workflow.max_revisions == 2
    assert config.sequence_spec().output_profile == "source-records"


def test_workflow_section_overrides_defaults():
    config = TranslationConfig.from_dict({
        "workflow": {"batch_size": 10, "max_revisions": 3, "review_tokens": 8192},
        "sequence": {"translation_concurrency": 5},
    })
    assert config.workflow.batch_size == 10
    assert config.workflow.max_revisions == 3
    assert config.workflow.review_tokens == 8192
    assert config.workflow.translation_concurrency == 5
