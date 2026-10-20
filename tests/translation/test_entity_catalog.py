"""Bounded, provenance-preserving entity catalog and prompt projections."""
from copy import deepcopy
from types import SimpleNamespace
import json

import pytest

from lightnovel_translation import EditionIndexSpec, ModelCapacity, RoleBudget
from translation.narrative.tokens import TokenCounter
from translation.narrative.schemas import NarrativeError
from translation.sequence.entities import (
    _candidate_rows, _core_candidate_projection, _decode_json,
    _entity_group_key, _select_core, entity_catalog_for_prompt, read_entity_catalog,
    read_entity_evidence, update_entity_catalog, _visible_entity_nodes,
)
from translation.storage import digest


def _record(name, kind, description, target_form=(), ident='entity'):
    fields = {'name': [name], 'kind': [kind], 'description': [description],
              'target_form': list(target_form)}
    return {'id': ident, 'type': 'ENTITY', 'fields': fields,
            'authority': 'candidate_unreviewed'}


class _Store:
    def __init__(self):
        self.data = {}

    def objects(self, kind):
        return list(self.data.get(kind, {}).items())

    def put(self, kind, value):
        ident = kind + ':' + digest(value)[:24]
        self.data.setdefault(kind, {})[ident] = deepcopy(value)
        return ident


class _Repo:
    def __init__(self):
        self.run_id = 'entity-test-run'
        self.store = _Store()


class _View:
    target_order = 100

    def visible_sources(self):
        return [{'chapter_id': f'c{i}', 'order': i, 'title': f'Chapter {i}'}
                for i in range(1, 100)]

    def read_chapters(self, _ids):
        return []


class _EvidenceView(_View):
    def visible_sources(self):
        return [{'chapter_id': 'chapter-near', 'order': 1, 'title': '近章'}]

    def read_chapters(self, _ids):
        return [{'source': {'chapter_id': 'chapter-near', 'title': '近章',
                            'text': '近章日文原文证据。'},
                 'translation': [{'text': '邻近中文参考。'}]}]


class _BoundaryView(_View):
    target_order = 2

    def visible_sources(self):
        return [{'chapter_id': 'c1', 'order': 1, 'title': '已知章节'},
                {'chapter_id': 'c2', 'order': 2, 'title': '目标章节'},
                {'chapter_id': 'c3', 'order': 3, 'title': '未来章节'}]


class _Counter:
    identity = {'method': 'test-byte-count'}

    def count(self, text):
        return len(text.encode('utf-8'))

    def messages(self, messages):
        return 32 + sum(self.count(message['content']) + 32 for message in messages)


class _SelectingCaller:
    def __init__(self):
        self.counter = _Counter()
        self.requests = []

    def call(self, task, kind, messages, *, role):
        self.requests.append({'task': task, 'kind': kind, 'role': role,
                              'messages': deepcopy(messages)})
        task_data = json.loads(messages[-1]['content'].split('\n', 1)[1].split('\n只返回JSON：', 1)[0])
        ids = [row['id'] for row in task_data['candidate_entities'][:task_data['maximum_items']]]
        return {'generation': {'text': json.dumps({'entity_ids': ids}, ensure_ascii=False)}}


def _selector_spec(*, input_tokens=9000, threshold=50, core_limit=50):
    return SimpleNamespace(
        entity_catalog_threshold=threshold,
        core_entity_limit=core_limit,
        n_chapter=1,
        n_child=1,
        max_attempts=2,
        capacity=ModelCapacity(30000, 1024, max_input_tokens=input_tokens,
                               token_inflation_factor=1, safety_tokens=0),
        roles={'indexer': RoleBudget(512)},
    )


def test_entity_observations_dedupe_identical_claims_but_keep_member_provenance():
    record = _record('ノア', 'person', '主角，来自边境村庄。', ['诺亚'], 'same-record')
    nodes = [
        {'node_id': 'l1', 'id': 'revision-l1', 'level': 1,
         'source_ids': ['c1'], 'records': [record], 'references': {}},
        {'node_id': 'l2', 'id': 'revision-l2', 'level': 2,
         'source_ids': ['c1', 'c2'], 'records': [record], 'references': {}},
    ]
    rows = _candidate_rows(nodes)
    assert len(rows) == 1
    assert rows[0]['source_ids'] == ['c1', 'c2']
    assert rows[0]['member_ids'] == sorted(rows[0]['member_ids'])
    assert len(rows[0]['member_ids']) == 2


def test_same_surface_name_with_different_entity_kinds_stays_separate():
    repo = _Repo()
    nodes = [
        {'node_id': 'l1-person', 'id': 'r1', 'level': 1, 'source_ids': ['c1'],
         'summary': '诺亚是主角。', 'records': [_record('ノア', 'person', '主角。')], 'references': {}},
        {'node_id': 'l2-skill', 'id': 'r2', 'level': 2, 'source_ids': ['c2'],
         'summary': '诺亚是技能名。', 'records': [_record('ノア', 'skill', '一种技能。')], 'references': {}},
    ]
    spec = _selector_spec()
    ident, catalog = update_entity_catalog(repo, spec, _View(), _SelectingCaller(), nodes)
    assert ident
    assert len(catalog['entities']) == 2
    assert {row['fields']['kind'][0] for row in catalog['entities']} == {'person', 'skill'}
    assert len({_entity_group_key('诺亚', kind) for kind in ('person', 'skill')}) == 2


def test_core_selector_compares_every_candidate_in_one_budgeted_request():
    spec = _selector_spec(input_tokens=20000)
    caller = _SelectingCaller()
    candidates = []
    for i in range(55):
        candidates.append({
            'id': f'entity:{i:03d}',
            'fields': {'name': [f'人物{i}'], 'kind': ['person'],
                       'description': ['重要角色'], 'target_form': [f'名{i}']},
            'source_ids': [f'chapter-{i}'],
            'evidence': [{'excerpt': 'FULL-EVIDENCE-MUST-NOT-BE-IN-SELECTOR' * 30}],
        })
    nodes = [{'node_id': 'L1', 'level': 1, 'source_ids': ['chapter-near'],
              'summary': 'L1 全局摘要', 'end_position': 1},
             {'node_id': 'L2', 'level': 2, 'source_ids': ['chapter-near'],
              'summary': 'L2 邻近摘要', 'end_position': 1}]
    l1 = [{'node_id': 'L1', 'summary': 'L1 全局摘要'}]
    selected, status = _select_core(_Repo(), spec, caller, l1, nodes, candidates, _EvidenceView())
    assert len(selected) == 50
    assert len(set(selected)) == len(selected)
    assert status['model_calls'] == 1
    assert len(caller.requests) == 1
    request_text = json.dumps(caller.requests[0]['messages'], ensure_ascii=False)
    assert 'L1 全局摘要' in request_text and 'L2 邻近摘要' in request_text
    assert '近章日文原文证据' in request_text and '邻近中文参考' in request_text
    assert 'FULL-EVIDENCE-MUST-NOT-BE-IN-SELECTOR' not in request_text
    payload = json.loads(caller.requests[0]['messages'][-1]['content'].split('\n', 1)[1].split('\n只返回JSON：', 1)[0])
    assert len(payload['candidate_entities']) == len(candidates)
    assert {row['id'] for row in payload['candidate_entities']} == {row['id'] for row in candidates}


def test_core_selector_uses_the_same_prefix_when_candidate_list_needs_batches():
    spec = _selector_spec(input_tokens=2600)
    caller = _SelectingCaller()
    candidates = [{'id': f'entity:{i:03d}',
        'fields': {'name': [f'人物{i}'], 'kind': ['person'],
                   'description': ['重要角色描述'], 'target_form': [f'译名{i}']},
        'source_ids': [f'chapter-{i}']} for i in range(75)]
    l1 = [{'node_id': 'L1', 'summary': '完整共享L1摘要'}]
    l2 = [{'node_id': 'L2', 'summary': '邻近L2摘要'}]

    selected, status = _select_core(_Repo(), spec, caller, l1,
        [{'node_id': 'L1', 'level': 1, 'summary': '完整共享L1摘要', 'end_position': 1},
         {'node_id': 'L2', 'level': 2, 'summary': '邻近L2摘要', 'end_position': 1}],
        candidates, _View())

    assert len(selected) == 50
    assert status['model_calls'] == len(caller.requests) > 1
    for request in caller.requests:
        text = json.dumps(request['messages'], ensure_ascii=False)
        assert '完整共享L1摘要' in text
        assert '邻近L2摘要' in text


def test_core_selector_is_skipped_at_the_50_entry_limit():
    caller = _SelectingCaller()
    nodes = [{
        'node_id': f'l1-{i}', 'id': f'r{i}', 'level': 1, 'source_ids': [f'c{i + 1}'],
        'summary': f'第{i}章摘要', 'records': [_record(f'实体{i}', 'person', '人物。')],
        'references': {},
    } for i in range(50)]

    _, catalog = update_entity_catalog(_Repo(), _selector_spec(), _View(), caller, nodes)

    assert len(catalog['entities']) == 50
    assert catalog['core_status']['status'] == 'not_needed'
    assert catalog['core_entity_ids'] == [row['id'] for row in catalog['entities']]
    assert not caller.requests


def test_cached_core_selection_does_not_create_a_spurious_catalog_revision():
    repo, caller = _Repo(), _SelectingCaller()
    spec = _selector_spec(input_tokens=30000)
    nodes = [{
        'node_id': f'l1-{i}', 'id': f'r{i}', 'level': 1, 'source_ids': [f'c{i + 1}'],
        'summary': f'第{i}章摘要', 'records': [_record(f'实体{i}', 'person', '人物。')],
        'references': {},
    } for i in range(51)]

    _, first = update_entity_catalog(repo, spec, _View(), caller, nodes)
    first_call_count = len(caller.requests)
    _, repeated = update_entity_catalog(repo, spec, _View(), caller, nodes)

    assert first['revision'] == repeated['revision'] == 1
    assert first['fingerprint'] == repeated['fingerprint']
    assert len(repo.store.objects('sequence-entity-catalog')) == 1
    assert first_call_count >= 1
    assert len(caller.requests) == first_call_count


def test_core_selector_fails_closed_if_shared_prefix_cannot_fit():
    spec = _selector_spec(input_tokens=1000)
    caller = _SelectingCaller()
    candidates = [{'id': f'e{i}', 'fields': {'name': [f'名称{i}'],
                   'kind': ['other'], 'description': ['描述'], 'target_form': []}}
                  for i in range(51)]
    nodes = [{'node_id': 'L1', 'level': 1, 'source_ids': ['c1'],
              'summary': '历史摘要' * 2000, 'end_position': 1}]
    with pytest.raises(NarrativeError) as caught:
        _select_core(_Repo(), spec, caller, nodes, nodes, candidates, _View())
    assert caught.value.kind == 'entity_core_context_exceeded'
    assert not caller.requests


def test_core_selector_shortens_long_descriptions_without_dropping_ids():
    spec = _selector_spec(input_tokens=22000)
    caller = _SelectingCaller()
    candidates = [{'id': f'entity:{i:03d}',
        'fields': {'name': [f'名称{i}'], 'kind': ['person'],
                   'description': [f'描述{i}' + '背景' * 500],
                   'target_form': [f'译名{i}']},
        'evidence': [{'excerpt': 'private provenance'}]} for i in range(55)]
    selected, status = _select_core(_Repo(), spec, caller, [], [], candidates, _View())
    assert len(selected) == 50
    assert status['projection_level'] == 3
    payload = json.loads(caller.requests[0]['messages'][-1]['content'].split('\n', 1)[1].split('\n只返回JSON：', 1)[0])
    assert {row['id'] for row in payload['candidate_entities']} == {row['id'] for row in candidates}
    assert max(len(row['description']) for row in payload['candidate_entities']) <= 80


def test_incremental_same_name_catalog_reuses_prior_semantic_merge():
    class MergeCaller(_SelectingCaller):
        def call(self, task, kind, messages, *, role):
            self.requests.append({'task': task, 'kind': kind, 'role': role,
                                  'messages': deepcopy(messages)})
            section = messages[-1]['content'].split('名称组及其L2摘要、局部原文证据】\n', 1)[1]
            payload = json.loads(section.split('\n返回格式：', 1)[0])
            rows = payload['same_name_candidates']
            members = sorted({member for row in rows for member in row['member_ids']})
            output = {'name': rows[0]['name'], 'kind': rows[0]['kind'],
                'description': '累计作品内人物描述。',
                'target_forms': sorted({form for row in rows for form in row['target_forms']}),
                'member_ids': members, 'ambiguity': ''}
            return {'generation': {'text': json.dumps(output, ensure_ascii=False)}}

    repo, caller, view = _Repo(), MergeCaller(), _View()
    spec = SimpleNamespace(entity_catalog_threshold=50, core_entity_limit=50,
        n_chapter=1, n_child=1, max_attempts=2,
        capacity=ModelCapacity(8192, 1024, max_input_tokens=5000,
                               token_inflation_factor=1, safety_tokens=0),
        roles={'indexer': RoleBudget(512)})
    first = {'node_id': 'l1-a', 'id': 'r1', 'level': 1, 'source_ids': ['c1'],
        'summary': 'ノア在村中。', 'records': [_record('ノア', 'person', '在村的少年。', ['诺亚'], 'e1')],
        'references': {}}
    second = {'node_id': 'l1-b', 'id': 'r2', 'level': 1, 'source_ids': ['c2'],
        'summary': 'ノア前往王都。', 'records': [_record('ノア', 'person', '离开村庄的少年。', ['诺亚'], 'e2')],
        'references': {}}
    _, initial = update_entity_catalog(repo, spec, view, caller, [first])
    assert initial['revision'] == 1 and not caller.requests
    _, updated = update_entity_catalog(repo, spec, caller=caller, view=view, nodes=[first, second])
    assert updated['revision'] == 2
    assert len(caller.requests) == 1
    expected_members = {member for row in _candidate_rows([first, second])
                        for member in row['member_ids']}
    assert set(updated['entities'][0]['member_ids']) == expected_members
    before = len(caller.requests)
    _, reused = update_entity_catalog(repo, spec, view, caller, [first, second])
    assert reused['fingerprint'] == updated['fingerprint']
    assert len(caller.requests) == before


def test_fullwidth_json_punctuation_is_normalized_before_strict_validation():
    assert _decode_json('输出：｛＂entity_ids＂：［＂entity:1＂］｝') == {'entity_ids': ['entity:1']}


def test_future_citations_are_removed_before_entity_catalog_updates():
    early = _record('ノア', 'person', '村中で暮らす少年。', ident='early')
    early['fields']['ja'] = ['ja:c1']
    future = _record('ノア', 'person', '未来才公开的身份。', ident='future')
    future['fields']['ja'] = ['ja:c3']
    node = {'node_id': 'mixed-range-l2', 'id': 'revision', 'level': 2,
        'source_ids': ['c1', 'c3'], 'summary': '包含已知和未来情节的摘要',
        'records': [early, future],
        'references': {
            'ja:c1': {'chapter_id': 'c1', 'language': 'ja', 'text': 'ノア在村中。'},
            'ja:c3': {'chapter_id': 'c3', 'language': 'ja', 'text': 'ノア公开了隐藏身份。'},
        }}

    visible, allowed = _visible_entity_nodes([node], _BoundaryView())

    assert allowed == {'c1'}
    assert len(visible) == 1
    assert visible[0]['summary'] == ''
    assert visible[0]['source_ids'] == ['c1']
    assert len(visible[0]['records']) == 1
    assert visible[0]['records'][0]['fields']['description'] == ['村中で暮らす少年。']
    assert visible[0]['records'][0]['fields']['ja'] == ['ja:c1']
    assert set(visible[0]['references']) == {'ja:c1'}


def test_public_catalog_is_compact_and_evidence_is_read_by_entity_id():
    repo = _Repo()
    evidence = [{'ref': 'ja:c1', 'excerpt': '局部原文证据', 'text_hash': 'hash'}]
    entity = {'id': 'entity:one', 'type': 'ENTITY',
        'fields': {'name': ['ノア'], 'kind': ['person'], 'description': ['主角。'],
                   'target_form': ['诺亚']},
        'member_ids': ['member:one', 'member:two'], 'node_revisions': ['rev:one'],
        'evidence': evidence, 'source_ids': ['c1'], 'semantic_status': 'candidate_unreviewed'}
    repo.store.put('sequence-entity-catalog', {'run_id': repo.run_id, 'revision': 1,
        'fingerprint': 'f', 'entities': [entity], 'core_entity_ids': ['entity:one'],
        'core_status': {'status': 'not_needed'}, 'raw_observation_count': 2,
        'visible_source_ids': ['c1'], 'visibility_order': 1,
        'semantic_status': 'candidate_unreviewed'})

    catalog = read_entity_catalog(repo)
    assert catalog['entities'][0]['member_count'] == 2
    assert catalog['entities'][0]['evidence_count'] == 1
    assert 'member_ids' not in catalog['entities'][0]
    assert 'node_revisions' not in catalog['entities'][0]
    assert 'source_ids' not in catalog['entities'][0]
    assert 'evidence' not in catalog['entities'][0]
    assert catalog['entities'][0]['source_count'] == 1
    assert read_entity_evidence(repo, 'entity:one')['entity']['evidence'] == evidence


def test_catalog_and_evidence_reads_reject_catalogs_beyond_visible_boundary():
    repo = _Repo()
    entity = {'id': 'entity:future', 'type': 'ENTITY',
        'fields': {'name': ['ノア'], 'kind': ['person'], 'description': ['未来信息。']},
        'member_ids': ['member:future'], 'node_revisions': ['rev:future'],
        'evidence': [{'ref': 'ja:c3', 'excerpt': '未来情节'}], 'source_ids': ['c3']}
    repo.store.put('sequence-entity-catalog', {'run_id': repo.run_id, 'revision': 1,
        'fingerprint': 'future', 'entities': [entity], 'core_entity_ids': ['entity:future'],
        'core_status': {'status': 'not_needed'}, 'raw_observation_count': 1,
        'visible_source_ids': ['c1', 'c3'], 'visibility_order': 3,
        'semantic_status': 'candidate_unreviewed'})

    assert read_entity_catalog(repo, visible_source_ids={'c1'}, before_order=2)['status'] == 'not_built'
    assert read_entity_evidence(repo, 'entity:future', visible_source_ids={'c1'},
                                before_order=2)['status'] == 'not_found'


def test_translation_prompt_entity_cards_are_bounded_and_drop_provenance_payloads():
    repo = _Repo()
    entity = {'id': 'entity:long', 'type': 'ENTITY',
        'fields': {'name': ['ノア' * 200], 'kind': ['person' * 100],
            'description': ['人物描述。' * 1000],
            'target_form': [f'译名{i}' * 100 for i in range(20)],
            'ambiguity': ['存在歧义。' * 100]},
        'authority': 'candidate_unreviewed', 'semantic_status': 'candidate_unreviewed',
        'source_ids': [f'c{i}' for i in range(1000)], 'member_ids': ['member'] * 1000,
        'evidence': [{'excerpt': '原文' * 1000}] * 1000}
    repo.store.put('sequence-entity-catalog', {'run_id': repo.run_id, 'revision': 1,
        'fingerprint': 'long', 'entities': [entity], 'core_entity_ids': ['entity:long'],
        'core_status': {'status': 'selected'}, 'raw_observation_count': 1000,
        'visible_source_ids': ['c1'], 'visibility_order': 1,
        'semantic_status': 'candidate_unreviewed'})

    _, cards = entity_catalog_for_prompt(repo, [
        {'source_ids': ['c1'], 'ordered_source_ids': ['c1']}])

    card = cards[0]
    assert card['prompt_projection'] == 'entity-card-v1'
    assert len(card['fields']['name'][0]) <= 160
    assert len(card['fields']['kind'][0]) <= 64
    assert len(card['fields']['description'][0]) <= 320
    assert len(card['fields']['target_form']) <= 8
    assert max(len(form) for form in card['fields']['target_form']) <= 80
    assert len(card['fields']['ambiguity'][0]) <= 120
    assert not {'source_ids', 'member_ids', 'evidence'} & card.keys()
