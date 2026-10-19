"""Regression scenarios from the independent CLI continuation experiment."""
import json
import re
from dataclasses import replace

import pytest

from lightnovel_translation import GenerationResult, ModelCapacity, ReferenceConfig, TranslationConfig
from translation.editions.archive import EditionArchive
from translation.editions.context import compile_reference_packet
from test_library_api import setup, book, OfflineProvider


def test_cross_edition_without_event_match_still_includes_style_prose(tmp_path):
    project, source, provider = setup(tmp_path)
    with project:
        zh = project.import_book(book(tmp_path, 'zh'), edition_id='print', language='zh', paragraph_policy='blank_line')
        with project.create_job('style', source=source, chapters=source.chapters()[:1], references=[zh]) as job:
            plan = job.plan_next()
            docs = plan['context']['edition_reference_selection']['style_document_ids']
            assert docs
            full = '\n'.join(m['content'] for m in plan['context']['messages'])
            assert '诺亚回到了村子。' in full
            assert '不是平行译文' in full
            assert '不得用文库材料' in full
            assert not provider.requests
            assert not job.reference_report()['chapters'][0]['warnings']
            result = job.run()
            assert result.state == 'complete', result.error
            report = job.reference_report()['chapters'][0]
            assert report['actual_style_document_ids'] == docs
            review_request = next(r for r in provider.requests if r.task == 'p5_review')
            assert '诺亚回到了村子。' in '\n'.join(m['content'] for m in review_request.messages)


def test_history_only_never_uses_uncertified_fallback_prose(tmp_path):
    project, source, provider = setup(tmp_path)
    with project:
        zh = project.import_book(book(tmp_path, 'zh'), edition_id='print', language='zh', paragraph_policy='blank_line')
        index = project.index([source, zh])
        packet = compile_reference_packet(index._pipeline, source.chapters()[0].id)
        assert packet['style_documents'] == []
        assert not provider.requests


def test_related_block_prose_does_not_become_target_event_evidence(tmp_path):
    project, source, _ = setup(tmp_path)
    with project:
        zh = project.import_book(book(tmp_path, 'zh'), edition_id='print', language='zh', paragraph_policy='blank_line')
        index = project.index([source, zh])
        sn, rn = index.nodes(edition=source.id)[0], index.nodes(edition=zh.id)[-1]
        association = {'status':'complete', 'fingerprint':'test-related', 'result':{'associations':[
            {'source_node':sn['node_id'], 'reference_node':rn['node_id'], 'relation':'related_context'}]}}
        with project.create_job('related', source=source, chapters=source.chapters()[1:2],
                                references=[zh], index=index, associations=[association]) as job:
            plan = job.plan_next()['context']
            full = '\n'.join(m['content'] for m in plan['messages'])
            assert rn['entry_ids'][0] in plan['edition_reference_selection']['style_document_ids']
            # Background-related prose is supplied without asserting a target event match.
            payload = json.loads(next(b['text'] for b in plan['prompt_blocks']
                                      if b['kind']=='cross_edition_reference').split('\n',1)[1])
            assert payload['block_associations']['items'] == []
            assert '不是平行译文' in full


def test_prepare_references_uses_only_selected_nodes_and_reports_unmatched(tmp_path):
    class EmptyMatches(OfflineProvider):
        def generate(self, request):
            if request.task.startswith('edition-entity-matching:'):
                self.requests.append(request)
                return GenerationResult('@@SUMMARY\nreviewed_all: yes\n@@END', 'stop', 'offline', 'empty', {}, 0)
            return super().generate(request)
    project, source, _ = setup(tmp_path, provider=EmptyMatches())
    with project:
        zh = project.import_book(book(tmp_path, 'zh'), edition_id='print', language='zh', paragraph_policy='blank_line')
        index = project.index([source, zh])
        result = index.prepare_references(source.chapters()[:1], zh.chapters()[:1])
        assert result['status'] == 'complete', result
        assert len(result['source_nodes']) == len(result['reference_nodes']) == 1
        coverage = result['entity_matches'][0]['matching_coverage']
        assert coverage['unreported_source_entity_ids']
        assert not coverage['source_catalog_complete']


def test_explicit_style_sample_is_preserved_and_foreign_id_rejected(tmp_path):
    project, source, _ = setup(tmp_path)
    with project:
        zh = project.import_book(book(tmp_path, 'zh'), edition_id='print', language='zh', paragraph_policy='blank_line')
        with project.create_job('explicit', source=source, references=[zh], chapters=source.chapters()[:1],
                                style_reference_chapters=[zh.chapters()[-1]]) as job:
            assert zh.chapters()[-1].id in job.plan_next()['context']['edition_reference_selection']['style_document_ids']
        with pytest.raises(ValueError, match='Chinese'):
            project.create_job('wrong', source=source, references=[zh], style_reference_chapters=[source.chapters()[0]])


class TerminologyProvider(OfflineProvider):
    def generate(self, request):
        if request.task.startswith('edition-terminology:'):
            self.requests.append(request)
            payload = json.loads(request.messages[-1]['content'])
            rows = [r for d in payload['reference_documents'] for r in d['paragraphs']]
            reference = next(r for r in rows if '巴鲁多' in r['text'])
            text = []
            for i, form in enumerate(payload['forms']):
                sr = next(r for r in payload['source_local_prose'] if form in r['text'])
                text.append(f"@@TERM t{i}\nsource_form: {form}\nsource_ref: {sr['ref']}\n"
                    f"target_form: 巴鲁多\nreference_ref: {reference['ref']}\nstatus: candidate\n"
                    "rendering_kind: name\ndescription: 治理魔族的重臣。\nreason: 两侧实际人物用例相符。")
            return GenerationResult('\n'.join(text)+'\n@@END', 'stop', 'offline', 'terms', {}, 0)
        return super().generate(request)


def test_unextracted_name_looks_up_unindexed_long_reference_chapter(tmp_path):
    config = TranslationConfig(references=ReferenceConfig(terminology_input_tokens=2500, terminology_radius=1))
    project, source, provider = setup(tmp_path, config=config, provider=TerminologyProvider())
    with project:
        ja = tmp_path/'names-ja.txt'
        ja.write_text('第1話 会議\n\n魔族を統治するバルトが報告した。', encoding='utf-8')
        source = project.import_book(ja, edition_id='names', language='ja', paragraph_policy='blank_line')
        path = tmp_path/'long-zh.txt'
        lines = ['无关的风景描写。'*8 for _ in range(300)]
        lines[210] = '巴鲁多是治理魔族的重臣。'
        path.write_text('第一章 会议\n\n'+'\n\n'.join(lines), encoding='utf-8')
        zh = project.import_book(path, edition_id='print', language='zh', paragraph_policy='blank_line')
        index = project.index([source, zh])
        result = index.resolve_terminology(source.chapters()[0], source_forms=['バルト'])
        assert result['status'] == 'complete', result
        assert result['items'][0]['target_form'] == '巴鲁多'
        assert result['partial_reference_documents']
        assert result['search_scope'] == [zh.chapters()[0].id]
        assert not result['trace'][0]['source_extracted']
        payload = json.loads(provider.requests[-1].messages[-1]['content'])
        assert len(payload['reference_documents'][0]['paragraphs']) < 300
        before = len(provider.requests)
        assert index.resolve_terminology(source.chapters()[0], source_forms=['バルト'])['status'] == 'complete'
        assert len(provider.requests) == before


def test_reference_limits_follow_capacity_and_honor_explicit_overrides():
    small = TranslationConfig()
    large = replace(small, capacity=ModelCapacity(1048576, 131072))
    a = small.references.budgets(small.capacity, small.role_budgets())
    b = large.references.budgets(large.capacity, large.role_budgets())
    assert b['packet_token_budget'] > a['packet_token_budget'] * 4
    assert b['terminology_input_tokens'] > a['terminology_input_tokens']
    custom = replace(large, references=ReferenceConfig(packet_token_budget=32000, terminology_input_tokens=5000))
    assert custom.public()['reference_budgets']['packet_token_budget'] == 32000
    assert custom.public()['reference_budgets']['terminology_input_tokens'] == 5000


def test_repeated_inspection_reuses_validation_but_mutation_is_detected(tmp_path, monkeypatch):
    project, source, _ = setup(tmp_path)
    with project:
        loads = []
        original = EditionArchive.from_folder.__func__
        def tracked(cls, folder):
            loads.append(folder)
            return original(cls, folder)
        monkeypatch.setattr(EditionArchive, 'from_folder', classmethod(tracked))
        source.chapters()
        count = len(loads)
        source.chapters()
        source.read(source.chapters()[0])
        assert len(loads) == count
        project.validate_inputs(force=True)
        assert len(loads) > count
        path = project.path/source.info['corpus_folder']/'paragraphs.jsonl'
        path.write_text(path.read_text(encoding='utf-8').replace('ノア', 'ノエ'), encoding='utf-8')
        with pytest.raises(ValueError, match='fingerprint'):
            source.chapters()


class BlockedProvider(OfflineProvider):
    def generate(self, request):
        if request.task == 'p5_review' and '修正后的译文。' not in '\n'.join(m['content'] for m in request.messages):
            self.requests.append(request)
            full = '\n'.join(m['content'] for m in request.messages)
            source = re.search(r'\[(C\w+\.J\d+)\]', full).group(1)
            target = re.search(r'\[(C\w+\.Z\d+)\]', full).group(1)
            text = (f'@@FINDING f1\ncategory: other\nseverity: major\nbasis: source_meaning\n'
                f'source_ref: {source}\ntarget_ref: {target}\nclaim: 本段含义错误。\n'
                'suggestion: 修正本段。\nconfidence: high\n@@SUMMARY\nreviewed_all: yes\n'
                'semantic_coverage_complete: yes\nzero_findings: no\n@@END')
            return GenerationResult(text, 'stop', 'offline', 'review', {}, 0)
        if request.task == 'p5_adjudication':
            self.requests.append(request)
            return GenerationResult('@@DECISION d1\nfinding: f1\ndecision: confirmed\nreason: 原文支持修正。\n@@END',
                                    'stop', 'offline', 'decision', {}, 0)
        return super().generate(request)


def test_agent_recovery_preserves_paid_work_and_rechecks_new_version(tmp_path):
    project, source, provider = setup(tmp_path, config=TranslationConfig(sequence={'max_revisions':0}),
                                       provider=BlockedProvider())
    with project:
        with project.create_job('recovery', source=source, chapters=source.chapters()[:1]) as job:
            assert job.run().state == 'blocked'
            draft = job.draft()
            assert draft and draft['review']
            attempts = job.status()['attempt_count']
            allowance = job.extend_revision_budget(reason='Agent 接管已确认问题。')
            assert job.status()['attempt_count'] == attempts
            assert job.repo.manifest['spec']['max_revisions'] == 0
            job.submit_revision({draft['paragraphs'][0]['alias']:'修正后的译文。'},
                                expected_version_id=draft['version_id'], reason='对照原段作局部修正。')
            assert job.status()['attempt_count'] == attempts
        with project.open_job('recovery') as job:
            assert job.draft()['revision_limit'] == 1
            result = job.run()
            assert result.state == 'complete', result.error
            assert job.translations()[0]['text'].startswith('修正后的译文。')
            assert allowance['extension_id'] in job.status()['chapters'][0]['revision_extension_ids']
            assert sum(r.task == 'p7_translation' for r in provider.requests) == 1


def test_extended_budget_resumes_automatic_revision_without_regenerating(tmp_path):
    class RevisingProvider(BlockedProvider):
        def generate(self, request):
            if request.task == 'p5_revision':
                self.requests.append(request)
                alias, expected = re.search(r'"target": "(C\w+\.Z\d+)", "expected_text_hash": "([a-f0-9]+)"',
                                            request.messages[-1]['content']).groups()
                return GenerationResult(f'@@PATCH p1\nstart: {alias}\nend: {alias}\n'
                    f'expected_text_hash: {expected}\nfindings: f1\nreplacement: 修正后的译文。\n@@END',
                    'stop', 'offline', 'revision', {}, 0)
            return super().generate(request)
    project, source, provider = setup(tmp_path, config=TranslationConfig(sequence={'max_revisions':0}),
                                       provider=RevisingProvider())
    with project:
        with project.create_job('automatic', source=source, chapters=source.chapters()[:1]) as job:
            assert job.run().state == 'blocked'
            before = len(provider.requests)
            assert job.resume().state == 'blocked'
            assert len(provider.requests) == before
            job.extend_revision_budget(reason='增加一次自动修订。')
        with project.open_job('automatic') as job:
            result = job.resume()
            assert result.state == 'complete', result.error
            assert sum(r.task == 'p7_translation' for r in provider.requests) == 1
            assert sum(r.task == 'p5_revision' for r in provider.requests) == 1
