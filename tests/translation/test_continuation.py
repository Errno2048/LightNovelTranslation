"""Ordering, scope isolation, evidence validation and durable continuation runs."""
import json
from dataclasses import replace
from pathlib import Path
import zipfile

import pytest

from lightnovel_translation import EditionIndexSpec, GenerationResult, TranslationConfig, TranslationProject
from lightnovel_translation.continuation import _reference_documents, parse_boundary, parse_navigation
from lightnovel_translation.ingestion import plan_reference_set
from translation.providers import ProviderError


def reference_files(tmp_path, volumes=(7, 8, 9)):
    paths = []
    for v in volumes:
        path = tmp_path / f'Novel {v}.txt'
        path.write_text(f'第{v}章 旅立ち\n\n诺亚离开村庄。\n\n伙伴带上行李。\n\n卷号{v}。', encoding='utf-8')
        paths.append(path)
    return paths


def test_numeric_order_and_missing_prefix_are_separate_from_input_positions(tmp_path):
    paths = reference_files(tmp_path)
    a = plan_reference_set(paths[::-1], edition_id='ref')
    b = plan_reference_set([paths[1], paths[0], paths[2]], edition_id='ref')
    assert a.fingerprint == b.fingerprint
    assert a.data['availability'] == {'scope': 'window', 'available_volumes': [7, 8, 9],
        'unavailable_prefix': [1, 2, 3, 4, 5, 6], 'internal_gaps': [], 'end_integrity': 'unknown'}
    assert [s.input_order for s in a.to_input_spec().sources] == [0, 1, 2]
    assert [s.volume_hint for s in a.to_input_spec().sources] == ['第7卷', '第8卷', '第9卷']
    gap = plan_reference_set([paths[2], paths[0]], edition_id='ref')
    assert gap.data['availability']['internal_gaps'] == [8]
    assert gap.data['ordered_sources'][-1]['volume_ordinal'] == 9


def test_prefix_natural_width_chinese_order_and_exclusion(tmp_path):
    paths = []
    for name in ('Novel 第十卷', 'Novel 第２卷', 'Novel 第一卷', 'Novel Ex'):
        p = tmp_path / (name + '.txt')
        p.write_text(name, encoding='utf-8')
        paths.append(p)
    plan = plan_reference_set(paths, edition_id='ref')
    assert [r['volume_ordinal'] for r in plan.data['ordered_sources']] == [1, 2, 10]
    assert plan.data['excluded_or_unplaced'][0]['volume_kind'] == 'extra'
    assert len(plan.to_input_spec().sources) == 3


def test_duplicates_and_unresolved_variants_are_not_silently_selected(tmp_path):
    p = reference_files(tmp_path, (7,))[0]
    duplicate = tmp_path / 'Novel 7-copy.txt'
    duplicate.write_bytes(p.read_bytes())
    a = plan_reference_set([duplicate, p], edition_id='ref')
    b = plan_reference_set([p, duplicate], edition_id='ref')
    assert a.fingerprint == b.fingerprint
    assert len(a.data['ordered_sources']) == 1
    variant = tmp_path / 'Novel 第7卷.txt'
    variant.write_text('different content', encoding='utf-8')
    plan = plan_reference_set([p, variant], edition_id='ref')
    assert plan.data['ordering_status'] == 'needs_resolution'
    with pytest.raises(ValueError, match='needs resolution'):
        plan.to_input_spec()


def test_package_filename_conflict_needs_explicit_override(tmp_path):
    p = tmp_path / 'Novel 7.epub'
    with zipfile.ZipFile(p, 'w') as z:
        z.writestr('META-INF/container.xml', '<container><rootfile full-path="book.opf"/></container>')
        z.writestr('book.opf', '<package><metadata><title>Novel 第8卷</title></metadata></package>')
    plan = plan_reference_set([p], edition_id='ref')
    assert plan.data['ordering_status'] == 'needs_resolution'
    resolved = plan_reference_set([p], edition_id='ref', overrides={p.name: {'volume_ordinal': 8}})
    assert resolved.to_input_spec().sources[0].volume_hint == '第8卷'
    assert resolved.data['ordered_sources'][0]['overrides'] == {'volume_ordinal': 8}


def test_different_series_are_not_sorted_into_a_single_reference(tmp_path):
    a, b = tmp_path / 'Novel 7.txt', tmp_path / 'Other 8.txt'
    a.write_text('first', encoding='utf-8')
    b.write_text('second', encoding='utf-8')
    plan = plan_reference_set([a, b], edition_id='ref')
    assert 'mixed_or_unresolved_series' in plan.data['issues']


def test_opaque_chapter_files_can_use_explicit_order_and_series_declaration(tmp_path):
    paths = [tmp_path / 'first.txt', tmp_path / 'second.txt']
    for i, path in enumerate(paths):
        path.write_text(str(i), encoding='utf-8')
    plan = plan_reference_set(paths[::-1], edition_id='ref', series_label='Declared work',
                              overrides={'first.txt': {'reading_order': 0}, 'second.txt': {'reading_order': 1}})
    assert [Path(s.path).name for s in plan.to_input_spec().sources] == ['first.txt', 'second.txt']


def test_navigation_never_accepts_unknown_chapter_ids():
    result = parse_navigation('@@CANDIDATE c1\nchapter_id: invented\nreason: same theme\n@@END', {'known'})
    assert not result['complete']


def test_narrative_endpoint_keeps_epilogues_but_excludes_author_notes_and_title_pages():
    class Archive:
        def documents(self, edition):
            return [{'raw_title': title} for title in ('Novel 9', '終章 帰還', '幕间 回忆', '后记')]
    docs = _reference_documents(Archive(), 'ref', {'ordered_sources': [{'path': 'Novel 9.epub'}]})
    assert [d['raw_title'] for d in docs] == ['終章 帰還', '幕间 回忆']


def boundary_text(source_ref='J1', reference_ref='Z1', source_quote='村を出る', reference_quote='离开村庄'):
    return f'''@@ANCHOR a1
source_ref: {source_ref}
reference_ref: {reference_ref}
source_quote: {source_quote}
reference_quote: {reference_quote}
relation: shared
thread: 主线
role: current
reason: 相同行动
@@BOUNDARY b1
status: suggested
chapter_id: next
end_anchor: a1
evidence_ref: J2
evidence_quote: 旅が始まる
reason: 同章开始新旅程
repeat_content: 无
missing_context: 无
state_compatible: yes
threads_checked: yes
@@END'''


def test_boundary_requires_real_quotes_and_complete_recommended_chapter():
    source = {'J1': {'text': '村を出る。', 'entry_id': 'old'}, 'J2': {'text': '旅が始まる。', 'entry_id': 'next'}}
    reference = {'Z1': {'text': '离开村庄。', 'entry_id': 'ref'}}
    good = parse_boundary(boundary_text(), source, reference, {'next'}, {'old', 'next', 'ref'})
    assert good['complete'], good
    assert not parse_boundary(boundary_text(source_quote='空想'), source, reference, {'next'}, {'next'})['complete']
    assert not parse_boundary(boundary_text(source_ref='unseen'), source, reference, {'next'}, {'next'})['complete']
    assert not parse_boundary(boundary_text(), source, reference, set(), {'next'})['complete']


class ContinuationProvider:
    def __init__(self):
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        payload = json.loads(request.messages[1]['content'].split('\n正文结束', 1)[0])
        if ':summary:' in request.task:
            events = []
            for doc in payload['documents']:
                for paragraph in doc['paragraphs'][:2]:
                    events.append(f"@@EVENT e{len(events)+1}\ntext: 离村准备。\nevidence_ref: {paragraph['ref']}")
            text = '@@OVERVIEW\ntext: 离村准备。\n' + '\n'.join(events) + '\n@@COVERAGE\nprocessed_all: yes\nentities_exhaustive: no\nomissions: 无\n@@END'
        elif ':navigation:' in request.task:
            cid = payload['source_directory'][3]['chapter_id']
            text = f'@@CANDIDATE c1\nchapter_id: {cid}\nthread: 主线\nreason: 查证旅立ち\n@@END'
        elif ':boundary:' in request.task:
            cid = payload['fully_read_source_chapters'][len(payload['fully_read_source_chapters'])//2]['chapter_id']
            source = next(d['paragraphs'] for d in payload['passages']['source'] if d['chapter_id'] == cid)
            ref = [r for d in payload['passages']['reference'] for r in d['paragraphs']]
            lines = []
            for i in range(2):
                lines.append(f"@@ANCHOR a{i+1}\nsource_ref: {source[i]['ref']}\nreference_ref: {ref[i]['ref']}\n"
                    f"source_quote: {source[i]['text']}\nreference_quote: {ref[i]['text']}\nrelation: shared\nthread: 主线\nrole: current\nreason: 行动相同")
            lines.append(f"@@BOUNDARY b1\nstatus: suggested\nchapter_id: {cid}\nend_anchor: a2\n"
                f"evidence_ref: {source[0]['ref']}\nevidence_quote: {source[0]['text']}\nreason: 新展开\nrepeat_content: 无\n"
                'missing_context: 无\nstate_compatible: yes\nthreads_checked: yes')
            text = '\n'.join(lines) + '\n@@END'
        else:
            raise AssertionError(request.task)
        return GenerationResult(text, 'stop', 'offline', str(len(self.requests)), {}, 0)


def setup_project(tmp_path, volumes, provider=None):
    provider = provider or ContinuationProvider()
    project = TranslationProject(tmp_path / 'project', TranslationConfig(
        index=EditionIndexSpec(child_max_chapters=2, parent_max_chapters=4)), provider=provider, provider_id='continuation-test-v1')
    source_file = tmp_path / 'web.txt'
    source_file.write_text('\n\n'.join(f'第{i}話 旅立ち\n\nノアは村を出る。\n\n皆は荷物を運ぶ。\n\n旅が始まる。' for i in range(1, 7)), encoding='utf-8')
    source = project.import_book(source_file, edition_id='web', language='ja', paragraph_policy='blank_line')
    plan = plan_reference_set(reference_files(tmp_path, volumes)[::-1], edition_id='ref')
    reference = project.import_edition(plan.to_input_spec())
    index = project.index([source, reference])
    return project, provider, source, reference, index, plan


@pytest.mark.parametrize('volumes,scope', [((1, 2, 3), 'prefix'), ((7, 8, 9), 'window')])
def test_public_continuation_end_to_end_with_cache_reuse_and_scope(tmp_path, volumes, scope):
    project, provider, source, reference, index, refs = setup_project(tmp_path, volumes)
    with project:
        plan = index.plan_continuation(source=source, references=[reference], reference_set=refs)
        assert not provider.requests
        assert plan['reference_endpoint']['volume_ordinal'] == volumes[-1]
        assert plan['reference_set']['availability']['scope'] == scope
        result = index.locate_continuation(plan)
        assert result['status'] == 'suggested', result
        assert result['recommended_start']['chapter_id'] == source.chapters()[3].id
        assert result['evidence_level'] == 'passage_checked'
        assert not result['search_coverage']['search_complete']
        assert Path(result['artifact_dir'], 'result.json').is_file()
        before = len(provider.requests)
        repeated = index.locate_continuation(plan)
        assert len(provider.requests) == before
        assert repeated['recommended_start'] == result['recommended_start']
        if scope == 'window':
            for request in provider.requests:
                payload = json.loads(request.messages[1]['content'].split('\n正文结束', 1)[0])
                assert '第1卷' not in json.dumps(payload, ensure_ascii=False)


def test_wrong_reference_manifest_and_tampered_plan_are_rejected_before_calls(tmp_path):
    project, provider, source, reference, index, refs = setup_project(tmp_path, (7, 8, 9))
    with project:
        refs.data['ordered_sources'].reverse()
        with pytest.raises(ValueError, match='fingerprint'):
            index.plan_continuation(source=source, references=[reference], reference_set=refs)
        assert not provider.requests


def test_request_budget_returns_evidence_gap_and_never_silently_restarts(tmp_path):
    project, provider, source, reference, index, refs = setup_project(tmp_path, (7, 8, 9))
    with project:
        plan = index.plan_continuation(source=source, references=[reference], reference_set=refs)
        result = index.locate_continuation(plan, max_requests=1)
        assert result['status'] == 'insufficient_evidence'
        assert result['limit_reason'] == 'budget'
        assert len(provider.requests) == 1
        index.locate_continuation(plan, max_requests=1)
        assert len(provider.requests) == 1


class TruncatedBoundaryProvider(ContinuationProvider):
    def __init__(self, *, invalid_repair=False, visible_partial=True):
        super().__init__()
        self.invalid_repair = invalid_repair
        self.visible_partial = visible_partial

    def generate(self, request):
        repairing = ':boundary-repair:' in request.task
        if ':boundary:' not in request.task and not repairing:
            return super().generate(request)
        self.requests.append(request)
        payload = json.loads(request.messages[1]['content'])
        cid = payload['fully_read_source_chapters'][0]['chapter_id']
        source = next(d['paragraphs'] for d in payload['passages']['source'] if d['chapter_id'] == cid)
        reference = payload['passages']['reference'][0]['paragraphs']
        anchor = (f"@@ANCHOR a1\nsource_ref: {source[0]['ref']}\nreference_ref: {reference[0]['ref']}\n"
                  f"source_quote: {source[0]['text']}\nreference_quote: {reference[0]['text']}\n"
                  'relation: partial\nthread: 主线\nrole: current\nreason: 仅有局部对应\n')
        boundary = (f"@@BOUNDARY b1\nstatus: suggested\nchapter_id: {cid}\nend_anchor: a1\n"
                    f"evidence_ref: {source[0]['ref']}\nevidence_quote: {source[0]['text']}\n")
        if not repairing:
            raise ProviderError('truncated', 'output exhausted', details={
                'text': anchor + boundary if self.visible_partial else ''})
        assert 'previous_truncated_output_candidate_only' in payload
        assert len(payload['fully_read_source_chapters']) == 1
        assert 'summaries' not in payload
        if self.invalid_repair:
            anchor = anchor.replace(f"source_quote: {source[0]['text']}", 'source_quote: invented quotation')
        return GenerationResult(anchor + boundary + 'reason: 局部证据支持候选\nrepeat_content: 部分重复\n'
            'missing_context: 其他叙事线未核验\nstate_compatible: unknown\nthreads_checked: no\n@@END',
            'stop', 'offline', str(len(self.requests)), {}, 0)


@pytest.mark.parametrize('invalid_repair,visible_partial', [(False, True), (True, True), (False, False)])
def test_truncated_boundary_repair_is_bound_to_real_evidence_and_durable(tmp_path, invalid_repair, visible_partial):
    provider = TruncatedBoundaryProvider(invalid_repair=invalid_repair, visible_partial=visible_partial)
    project, _, source, reference, index, refs = setup_project(tmp_path, (7, 8, 9), provider)
    with project:
        plan = index.plan_continuation(source=source, references=[reference], reference_set=refs)
        result = index.locate_continuation(plan)
        assert not result['auto_eligible']
        if invalid_repair:
            assert result['status'] == 'insufficient_evidence'
            assert result['limit_reason'] == 'boundary_failed'
            assert result['recommended_start'] is None
        else:
            assert result['status'] == 'suggested', result
            assert result['last_shared_anchors'][0]['relation'] == 'partial'
            assert result['boundary']['state_compatible'] == 'unknown'
            assert result['boundary']['threads_checked'] == 'no'
            assert result['steps'][-1]['repair_scope'] == 'bound_passages_and_candidate_chapter'
            assert result['steps'][-1]['visible_truncated_output'] == visible_partial
        before = len(provider.requests)
        repeated = index.locate_continuation(plan)
        assert len(provider.requests) == before
        assert repeated['status'] == result['status']


def test_lookup_budget_preserves_prior_verified_anchors_without_claiming_success(tmp_path):
    class LookupProvider(ContinuationProvider):
        def generate(self, request):
            result = super().generate(request)
            if ':boundary:' in request.task:
                payload = json.loads(request.messages[1]['content'])
                full = {d['chapter_id'] for d in payload['fully_read_source_chapters']}
                target = next(d['chapter_id'] for d in reversed(payload['source_directory']) if d['chapter_id'] not in full)
                result = replace(result, text=result.text.replace('@@END',
                    f'@@REQUEST r1\nchapter_id: {target}\nreason: 核对后一章\n@@END'))
            return result
    provider = LookupProvider()
    project, _, source, reference, index, refs = setup_project(tmp_path, (7, 8, 9), provider)
    with project:
        plan = index.plan_continuation(source=source, references=[reference], reference_set=refs)
        result = index.locate_continuation(plan, max_requests=4)
        assert result['status'] == 'insufficient_evidence', result
        assert result['limit_reason'] == 'budget'
        assert result['recommended_start'] is None
        assert result['candidate_start'] is not None
        assert len(result['last_shared_anchors']) == 2
        assert result['search_coverage']['remaining_requests']
