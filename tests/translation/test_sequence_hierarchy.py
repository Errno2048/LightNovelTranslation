"""Direct-first history bootstrap, lazy details and continuous coverage."""
from collections import Counter
from dataclasses import replace
import json

import pytest

from lightnovel_translation import (EditionIndexSpec, GenerationResult, Limits,
    ModelCapacity, RoleBudget, TranslationConfig, TranslationError, TranslationProject)
from translation.providers import ProviderError
from translation.sequence.hierarchy import latest_incremental_nodes, latest_nodes, plan_hierarchy
from translation.storage import digest
from test_library_api import OfflineProvider, book


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv('LIGHTNOVEL_TRANSLATION_HOME', str(tmp_path/'no-credentials'))


def make_history(tmp_path, *, provider=None, history=12, total=22, config=None, mode='parallel'):
    provider = provider or OfflineProvider()
    config = config or TranslationConfig(
        index=EditionIndexSpec(parent_max_chapters=6, child_max_chapters=2),
        sequence={'n_chapter':2, 'n_child':1})
    project = TranslationProject(tmp_path/'project', config, provider=provider, provider_id='offline-history')
    source = project.import_book(book(tmp_path, count=total), edition_id='ja', language='ja', paragraph_policy='blank_line')
    reference = project.import_book(book(tmp_path, 'zh', count=total), edition_id='zh', language='zh', paragraph_policy='blank_line')
    ja,zh=source.chapters(),reference.chapters()
    kwargs = {'references':[reference], 'parallel_pairs':{ja[i].id:[zh[i].id] for i in range(history)}} if mode=='parallel' else {}
    job = project.create_job('sequence',source=source,chapters=ja[history:],mode=mode,**kwargs)
    return project,source,reference,job,provider


def assert_cover(context, expected):
    counts=Counter(cid for row in context['coverage'] for cid in row['source_ids'])
    assert counts==Counter(expected)
    assert all(row['representation']!='explicit_gap' for row in context['coverage'])


def test_parallel_history_direct_first_far_children_lazy_and_no_answers(tmp_path):
    project,source,reference,job,provider=make_history(tmp_path)
    with project,job:
        plan=job.plan_next()
        assert plan['state']=='index_required'
        assert plan['historical_index']['pending'] and not provider.requests
        ready=job.prepare_history()
        assert ready['historical_index']['ready']
        nodes=latest_nodes(job.repo)
        assert len([n for n in nodes if n['level']==1])==2
        assert len([n for n in nodes if n['level']==2])==1
        assert all(n['build_method']=='direct' for n in nodes)
        full='\n'.join(m['content'] for m in provider.requests[0].messages)
        assert '诺亚回到了村子' in full
        allowed={c.id for c in source.chapters()[:12]}|{c.id for c in reference.chapters()[:12]}
        assert all(d['chapter_id'] in allowed for n in nodes for d in n['dependencies'])
        context=job.plan_next()['context']
        assert_cover(context,[c.id for c in source.chapters()[:12]])
        assert {r['representation'] for r in context['coverage']}=={'L1','L2','full_pair'}
        navigation=next(m['content'] for m in context['messages']
            if m['role']=='user' and '历史层次导航' in m['content'])
        entries=json.loads(navigation.split('\n',1)[1])
        assert {e['kind'] for e in entries}=={'L1','L2'}
        assert all(e['kind']=='L'+str(e['level']) for e in entries)
        assert any(detail['status']=='not_built' for entry in entries for detail in entry.get('detail_nodes', []))
        before=len(provider.requests)
        job.prepare_history()
        assert len(provider.requests)==before
        assert job.run(max_chapters=1).committed_count==1


def test_source_only_history_needs_no_reference_alignment(tmp_path):
    project,source,_,job,provider=make_history(tmp_path,mode='source_only')
    with project,job:
        assert job.plan_next()['state']=='index_required'
        job.prepare_history()
        context=job.plan_next()['context']
        assert_cover(context,[c.id for c in source.chapters()[:12]])
        assert any(r['representation']=='full_source' for r in context['coverage'])
        assert not any(d['chapter_id'].startswith('generated:') for d in context['dependencies'])
        for request in provider.requests:
            assert '诺亚回到了村子。' not in '\n'.join(m['content'] for m in request.messages)


def test_lazy_detail_only_builds_requested_child_and_preserves_old_checkpoint(tmp_path):
    project,source,_,job,provider=make_history(tmp_path)
    with project,job:
        job.prepare_history()
        old_id=job.repo.heads()['checkpoint']; old=job.repo.store.get(old_id)
        plan=plan_hierarchy(job.repo,job.engine.spec,job.engine.view(source.chapters()[12].id))
        child=next(n for n in plan['nodes'] if n['level']==2)
        before=len(provider.requests)
        job.ensure_details([child['node_id']])
        assert len(provider.requests)==before+1
        assert job.repo.store.get(old_id)==old
        assert job.repo.heads()['checkpoint']!=old_id
        job.ensure_details([child['node_id']])
        assert len(provider.requests)==before+1
        with pytest.raises(TranslationError):
            job.ensure_details(['unknown-future-child'])


def test_continuous_window_crosses_child_and_parent_without_duplicate_coverage(tmp_path):
    project,source,_,job,provider=make_history(tmp_path)
    with project,job:
        job.prepare_history()
        first_layout=list(job.repo.store.objects('sequence-hierarchy-plan'))[0][1]
        first_committed_turn = None
        for i in range(8):
            result=job.run(max_chapters=1)
            assert result.ok,result.to_dict()
            if i == 0:
                first_committed_turn = job.repo.commits()[0][1]['translation_turn']['user']['content']
            job.checkpoint()
            context=job.plan_next()['context']
            assert_cover(context,[c.id for c in source.chapters()[:13+i]])
            if i >= 2:
                # Older committed chapters must leave the literal turn history
                # once their sealed L2 is summarized into far L1 navigation.
                assert latest_incremental_nodes(job.repo)
                assert any(row['representation']=='L1' for row in context['coverage'])
                assert first_committed_turn not in '\n'.join(m['content'] for m in context['messages'])
                assert sum(m['role']=='assistant' for m in context['messages']) <= job.engine.spec.n_chapter
                assert any(n['level'] == 1 and n.get('build_method') == 'direct'
                           for n in latest_incremental_nodes(job.repo))
        assert list(job.repo.store.objects('sequence-hierarchy-plan'))[0][1]==first_layout
        assert all(n['end_position']<source.chapters()[20].position for n in latest_nodes(job.repo))


def test_run_automatically_rebuilds_hierarchy_after_commits(tmp_path):
    config = TranslationConfig(index=EditionIndexSpec(parent_max_chapters=6, child_max_chapters=5),
                               sequence={'n_chapter': 2, 'n_child': 1})
    project, source, _, job, provider = make_history(tmp_path, config=config)
    with project, job:
        job.prepare_history()
        result = job.run(max_chapters=4)
        assert result.ok, result.to_dict()
        plan = job.plan_next()
        assert plan['needs_checkpoint']
        assert_cover(plan['context'], [chapter.id for chapter in source.chapters()[:16]])
        first_turn = job.repo.commits()[0][1]['translation_turn']['user']['content']
        result = job.run(max_chapters=1)
        assert result.ok, result.to_dict()
        request = next(request for request in reversed(provider.requests)
                       if request.task == 'p7_translation')
        assert sum(message['role'] == 'assistant' for message in request.messages) <= job.engine.spec.n_chapter
        assert first_turn not in '\n'.join(message['content'] for message in request.messages)
        context = job.plan_next()['context']
        assert any(row['representation'] == 'L1' for row in context['coverage'])
        assert_cover(context, [chapter.id for chapter in source.chapters()[:17]])


def test_incremental_parent_tries_direct_then_rolls_up_only_on_capacity(tmp_path):
    class DirectTooLarge(OfflineProvider):
        def __init__(self):
            super().__init__()
            self.extraction_levels = []

        def generate(self, request):
            if request.task.startswith('p6_extraction'):
                self.extraction_levels.append('L1' if 'L1' in request.messages[-1]['content'] else 'L2')
            if request.task.startswith('p6_extraction') and '节点层级为 L1' in request.messages[-1]['content']:
                self.requests.append(request)
                raise ProviderError('parameters', 'too long', False,
                    {'message': 'maximum context length exceeded'})
            return super().generate(request)

    config = TranslationConfig(index=EditionIndexSpec(parent_max_chapters=6, child_max_chapters=2),
                               sequence={'n_chapter': 2, 'n_child': 1,
                                         'parent_build_mode': 'direct_then_rollup'})
    provider = DirectTooLarge()
    project, _, _, job, _ = make_history(tmp_path, provider=provider,
                                         history=0, total=9, config=config, mode='source_only')
    with project, job:
        result = job.run(max_chapters=6)
        assert result.ok, result.to_dict()
        provider.extraction_levels.clear()
        result = job.run(max_chapters=1)
        assert result.ok, result.to_dict()
        assert provider.extraction_levels and provider.extraction_levels[0] == 'L1'
        parents = [node for node in latest_incremental_nodes(job.repo) if node['level'] == 1]
        assert parents
        assert all(node['build_method'] == 'child_summary_rollup' for node in parents)
        assert all(node['fallback_reason'] for node in parents)


def test_independent_direct_parent_does_not_need_l2_and_is_cached(tmp_path):
    project,source,_,job,provider=make_history(tmp_path)
    with project,job:
        index=project.index([source])
        parent=index.nodes(edition='ja',level=1)[0]
        result=index.build_parent(parent['node_id'])
        assert result['build_method']=='direct' and result['coverage_complete']
        assert index.read(parent['node_id'])['status']=='found'
        assert all(index.read(cid)['status']=='not_built' for cid in parent['child_ids'])
        before=len(provider.requests)
        assert index.build_parent(parent['node_id'])['cache_hit']
        assert len(provider.requests)==before


def test_independent_parent_builds_l2_only_after_explicit_context_fallback(tmp_path):
    class DirectTooLarge(OfflineProvider):
        def generate(self, request):
            self.requests.append(request)
            if ':direct-v1:attempt:' in request.task:
                raise ProviderError('parameters', 'prompt too long', False,
                    {'message': 'maximum context length exceeded'})
            if request.task.startswith('parent-overview:'):
                return GenerationResult('诺亚回村并讲述旅途。', 'stop', 'offline', 'parent', {}, 0)
            self.requests.pop()
            return super().generate(request)

    project,source,_,_,provider=make_history(tmp_path,provider=DirectTooLarge())
    with project:
        index=project.index([source])
        parent=index.nodes(edition='ja',level=1)[0]
        result=index.build_parent(parent['node_id'], method='direct_then_rollup')
        assert result['build_method']=='child_summary_rollup'
        assert result['coverage_complete']
        assert result['fallback_reason']['kind']=='remote_context_limit'
        assert result['child_revisions']
        assert all(index.read(child)['status']=='found' for child in parent['child_ids'])
        assert sum(':direct-v1:attempt:' in request.task for request in provider.requests)==1


def test_independent_parent_default_stops_after_remote_overflow_without_building_l2(tmp_path):
    class DirectTooLarge(OfflineProvider):
        def generate(self, request):
            self.requests.append(request)
            if ':direct-v1:attempt:' in request.task:
                raise ProviderError('parameters', 'prompt too long', False,
                    {'message': 'maximum context length exceeded'})
            return super().generate(request)

    project,source,_,_,provider=make_history(tmp_path,provider=DirectTooLarge())
    with project:
        index=project.index([source])
        parent=index.nodes(edition='ja',level=1)[0]
        result=index.build_parent(parent['node_id'])
        assert result['status']=='blocked_context'
        assert result['fallback_reason']['kind']=='remote_context_limit'
        assert all(index.read(child)['status']=='not_built' for child in parent['child_ids'])
        assert sum(':direct-v1:attempt:' in request.task for request in provider.requests)==1
        assert not any(request.task.startswith('edition-index:B') and
                       ':direct-v1:attempt:' not in request.task for request in provider.requests)


def test_independent_partial_parent_is_not_reported_complete(tmp_path):
    class PartialParent(OfflineProvider):
        def generate(self, request):
            if ':direct-v1:attempt:' in request.task:
                self.requests.append(request)
                payload = json.loads(request.messages[-1]['content'].split('\n正文结束', 1)[0])
                ref = payload['documents'][0]['paragraphs'][0]['ref']
                text = (f'@@OVERVIEW\ntext: 仅覆盖已读取部分。\n@@EVENT e1\ntext: 旅人回村。\n'
                        f'evidence_ref: {ref}\n@@COVERAGE\nprocessed_all: no\nentities_exhaustive: no\n'
                        'omissions: 后半段未确认\n@@END')
                return GenerationResult(text, 'stop', 'offline', 'partial', {}, 0)
            return super().generate(request)

    project,source,_,_,provider=make_history(tmp_path,provider=PartialParent())
    with project:
        index=project.index([source])
        result=index.build(workers=2, include_parents=True)
        assert not result['complete']
        parent=index.nodes(edition='ja',level=1)[0]
        assert index.read(parent['node_id'])['status']=='partial'


@pytest.mark.parametrize('failure', ['protocol', 'server'])
def test_independent_non_capacity_parent_failure_does_not_build_l2(tmp_path, failure):
    class BadParent(OfflineProvider):
        def generate(self, request):
            if request.task.startswith('edition-index:') and ':direct-v1:attempt:' in request.task:
                self.requests.append(request)
                if failure == 'server':
                    raise ProviderError('server', 'offline failure')
                return GenerationResult('not a summary', 'stop', 'offline', 'bad', {}, 0)
            return super().generate(request)

    project,source,_,_,provider=make_history(tmp_path,provider=BadParent())
    with project:
        index=project.index([source])
        parent=index.nodes(edition='ja',level=1)[0]
        result=index.build_parent(parent['node_id'])
        assert result.get('status') in {'failed', 'blocked'}
        assert not any(request.task.startswith('edition-index:B') and
                       ':direct-v1:attempt:' not in request.task for request in provider.requests)
        assert all(index.read(child)['status']=='not_built' for child in parent['child_ids'])


@pytest.mark.parametrize('error', ['protocol','server'])
def test_bad_extraction_never_triggers_full_child_fallback(tmp_path,error):
    class Bad(OfflineProvider):
        def generate(self,request):
            if request.task.startswith('p6_extraction'):
                self.requests.append(request)
                if error=='server': raise ProviderError('server','offline failure')
                return GenerationResult('bad protocol','stop','offline','bad',{},0)
            return super().generate(request)
    project,_,_,job,provider=make_history(tmp_path,provider=Bad())
    with project,job:
        result=job.run(max_chapters=1)
        assert result.state=='blocked' and result.committed_count==0
        assert len(provider.requests)==3
        assert not latest_nodes(job.repo)
        before=len(provider.requests)
        assert job.resume(max_chapters=1).state=='blocked'
        assert len(provider.requests)==before


def test_remote_context_error_alone_triggers_rollup(tmp_path):
    class Capacity(OfflineProvider):
        def generate(self,request):
            if request.task.startswith('p6_extraction') and '本次节点层级为 L1' in request.messages[-1]['content']:
                self.requests.append(request)
                raise ProviderError('parameters','too long',False,
                    {'message':'maximum context length exceeded'})
            return super().generate(request)
    config = TranslationConfig(
        index=EditionIndexSpec(parent_max_chapters=6, child_max_chapters=2),
        sequence={'n_chapter': 2, 'n_child': 1,
                  'parent_build_mode': 'direct_then_rollup'})
    project,_,_,job,provider=make_history(tmp_path,provider=Capacity(),config=config)
    with project,job:
        ready=job.prepare_history()
        parents=[n for n in latest_nodes(job.repo) if n['level']==1]
        assert len(parents)==2
        assert all(n['build_method']=='child_summary_rollup' for n in parents)
        assert all(n['child_revisions'] and n['fallback_reason'] for n in parents)
        assert ready['historical_index']['ready']
        before=len(provider.requests)
        job.prepare_history()
        assert len(provider.requests)==before


def test_independent_minimum_l1_capacity_decision_is_reported_before_generation(tmp_path):
    capacity = ModelCapacity(8192, 4096, max_input_tokens=1000, safety_tokens=0)
    config = TranslationConfig(capacity=capacity,
        roles={'indexer': RoleBudget(1024)},
        index=EditionIndexSpec(parent_max_chapters=6, child_max_chapters=2,
                               parent_min_chapters=3))
    provider=OfflineProvider()
    project=TranslationProject(tmp_path/'capacity-project',config,provider=provider,
                               provider_id='offline-capacity')
    source=project.import_book(book(tmp_path,count=8),edition_id='ja',language='ja',
                              paragraph_policy='blank_line')
    with project:
        index=project.index([source])
        plan=index.plan()['plans'][0]
        assert plan['planning']['status'] in {'index_capacity_decision_required','oversized_chapter'}
        assert any(row['kind']=='minimum_l1_does_not_fit'
                   for row in plan['planning']['diagnostics'])
        parent=index.nodes(edition='ja',level=1)[0]
        result=index.build_parent(parent['node_id'])
        assert result['status']=='index_capacity_decision_required'
        assert result['not_sent']
        assert not provider.requests
