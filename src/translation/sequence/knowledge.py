"""Joint bilingual L2 extraction and deterministic provenance-preserving rollup."""
import json

from ..mvp.recovery import ExtractionRecovery
from ..narrative.extraction import SYSTEM, SUMMARY_FORMAT, EXTRACT_FORMAT, parse_child, PARSER_VERSION
from ..narrative.extraction import STABLE_PARSER_VERSION
from ..references import STABLE, chapter_ref, pair_rows, prompt_projection
from ..narrative.partition import AdaptiveIndexSpec, plan_adaptive_index
from ..narrative.service import dependency
from ..narrative.schemas import NarrativeError
from ..narrative.length import summary_length_info
from ..storage import digest
from .requests import sequence_limits
from .requests import SequenceCaller


def parser(refs):
    def parse(raw):
        p=parse_child(raw,refs)
        summaries=[r for r in p['accepted'] if r['type']=='SUMMARY']
        summary=summaries[0]['fields']['text'][0] if len(summaries)==1 else None
        records=p['accepted']+p['rejected']
        return {'complete':bool(p['complete'] and summary and not p['issues']), 'summary':summary,
                'records':p['accepted'],'rejected':p['rejected'],
                'payload':[{'type':r['type'],'id':r['id'],'fields':r['fields'],
                            'original_citations':r.get('original_citation_fields')} for r in sorted(records,key=lambda r:r['line'])],
                'errors':p['issues']+([] if p['complete'] else ['missing_end'])}
    return parse


def render_pairs(pairs, *, reference_profile='legacy'):
    refs={}; lines=[]
    for pair in pairs:
        source=pair['source']; pos=source['order']
        if reference_profile == STABLE:
            lines.append(f"【章节 {chapter_ref(source)}：{source['title']}】")
            rows, bindings = pair_rows(pair)
            if refs.keys() & bindings.keys():
                raise ValueError('duplicate canonical chapter in extraction input')
            refs.update(bindings)
            lines.extend(f"[{r['alias']}] {r['text']}" for r in rows)
            continue
        lines.append(f"【阅读位置 {pos} / {source['match_key']}：{source['title']}】")
        for language,tag,docs in [('ja','J',[source]),('zh','Z',pair['translation'])]:
            i=0
            for doc in docs:
                for pid,text in zip(doc['paragraph_ids'],doc['paragraph_texts']):
                    ref=f'{tag}{pos}.{i}'; i+=1
                    refs[ref]={'language':language,'chapter':pos,'chapter_id':doc['chapter_id'],
                        'paragraph_id':pid,'text':text,'revision_id':doc['revision_id']}
                    lines.append(f'[{ref}] {text}')
    return '\n'.join(lines),refs


def joint_messages(pairs,history,previous, *, reference_profile='legacy'):
    """Use the same evidence protocol for production and frozen probes."""
    source,refs=render_pairs(pairs, reference_profile=reference_profile)
    stable = reference_profile == STABLE
    if stable:
        docs = [d for p in pairs for d in [p['source']]+p['translation']]
        history = prompt_projection(history, docs)
        previous = prompt_projection(previous, docs)
    extract_format = (EXTRACT_FORMAT.replace('disclosed_at: 披露章节数字',
        'disclosed_in: 支持该记录的章节ID，例如C0400；跨章用逗号分隔已有ID，不写数字区间')
        if stable else EXTRACT_FORMAT)
    messages=[{'role':'system','content':SYSTEM},
        {'role':'user','content':'【已披露历史一级导航：未审，不可作为当前引用】\n'+json.dumps(history,ensure_ascii=False)},
        {'role':'user','content':'【同父前驱L2摘要；仅导航，不是当前证据】\n'+json.dumps(previous,ensure_ascii=False)},
        {'role':'user','content':source},
        {'role':'user','content':'任务 JOINT：只总结和抽取当前子块。日文是事实依据；中文只证明本分支实际译法，均是provisional_rendering。'
            + ('disclosed_in只复制当前章节ID；ja/zh只复制当前C章节.J/Z段落ID，不写目录序号或爬虫编号。'
               if stable else 'disclosed_at使用上方阅读位置数字，不用标题中的章号。')+
            '摘要不超过2500字，不设下限。仅引用当前给定段落，不从前情编造事件。\n'+
            SUMMARY_FORMAT.replace('@@END','')+'\n'+extract_format}]
    return messages,refs


def node_is_valid(repo,node):
    if node['run_id']!=repo.run_id:
        raise NarrativeError('visibility_denied','index node belongs to another branch')
    for ident in node.get('commit_ids',[]): repo.validate_commit(ident)
    return True


def current_index(repo,spec,view):
    pairs=view.read_chapters([c['source_id'] for _,c in repo.all_commits()])
    old=[x for _,x in repo.store.objects('sequence-index') if x['run_id']==repo.run_id and
         len(x['plan']['ordered_source_ids'])<=len(pairs)]
    previous=max(old,key=lambda x:len(x['plan']['ordered_source_ids']),default=None)
    index_spec=AdaptiveIndexSpec(parent_max_chapters=spec.parent_max_chapters,
        child_max_chapters=spec.child_max_chapters,planning_utilization=spec.planning_utilization,
        child_min_content_tokens=spec.child_min_content_tokens,
        parent_summary_input_tokens_per_child=spec.parent_summary_input_tokens_per_child)
    plan=plan_adaptive_index(pairs,index_spec,spec.capacity,spec.roles['indexer'],view.counter,
        source_manifest_id=repo.manifest['frozen_input_hash'],visibility_policy_id=repo.run_id,
        existing_plan=previous['plan'] if previous else None)
    repo.store.put('sequence-index',{'run_id':repo.run_id,'plan':plan})
    return plan


def _find_materialized_parent(repo, view, root):
    """Return an exact cached L1 revision without consulting child nodes."""
    from .hierarchy import usable
    ids = root['ordered_source_ids']
    pair_hash = digest(view.read_chapters(ids))
    for ident, node in repo.store.objects('sequence-node'):
        if (node.get('run_id') == repo.run_id and node['node_id'] == root['node_id']
                and node['level'] == 1 and node['pair_hash'] == pair_hash and usable(node)):
            node_is_valid(repo, node)
            return {'id': ident, **node}
    return None


def _materialize_parent_direct(repo, spec, view, provider, root, history):
    """Try the L1 request using the complete committed prefix only.

    The returned failure is intentionally limited to capacity failures by
    ``_extract``.  Callers can then decide whether to pay for L2 evidence and
    perform a rollup; no child node is a precondition for this request.
    """
    from .hierarchy import _extract, _save
    cached = _find_materialized_parent(repo, view, root)
    if cached:
        return cached, None
    ids = root['ordered_source_ids']
    payload, failure = _extract(repo, spec, view, provider, root, ids, history)
    if failure:
        return None, failure
    payload['open_prefix'] = root['state'] == 'open'
    return _save(repo, view, root, ids, payload, method='direct',
                 fallback_reason=None, child_revisions=[], store_kind='sequence-node'), None


def _materialize_parent_rollup(repo, view, caller, root, children, fallback_reason):
    """Roll up complete L2 coverage after an L1 capacity failure."""
    from .context import _parent_overview
    from .hierarchy import _save, usable
    ids = root['ordered_source_ids']
    usable_children = [node for node in children if usable(node)
                       and set(node['source_ids']) <= set(ids)]
    covered = [cid for node in usable_children for cid in node['source_ids']]
    if covered != ids:
        raise NarrativeError('incomplete_incremental_index',
            'L1 capacity fallback lacks usable L2 coverage: ' + root['node_id'])
    summary, summary_id = _parent_overview(repo, caller, usable_children, root['node_id'])
    payload = {'summary': summary, 'summary_revision': summary_id,
        'records': [record for node in usable_children for record in node['records']],
        'quarantined': [record for node in usable_children for record in node.get('quarantined', [])],
        'references': {key: value for node in usable_children
                       for key, value in node.get('references', {}).items()},
        'state': 'partial' if any(node['state'] == 'partial' for node in usable_children) else 'extracted',
        'failure_reason': None, 'open_prefix': root['state'] == 'open'}
    return _save(repo, view, root, ids, payload, method='child_summary_rollup',
                 fallback_reason=fallback_reason,
                 child_revisions=[node['id'] for node in usable_children],
                 store_kind='sequence-node')


def materialize_nodes(repo,spec,view,provider,*,force_open=False):
    if not repo.all_commits(): return []
    plan=current_index(repo,spec,view)
    recovery=ExtractionRecovery(repo.store,view.counter,provider,provider_identity=repo.manifest['provider'],
        limits=sequence_limits(spec),max_retries=spec.max_attempts-1,capacity=spec.capacity,
        max_output_tokens=spec.capacity.max_output_tokens)
    # Summary-rollup parents are durable logical tasks too. Reuse the same
    # ledger and attempt cap as translation/checkpoint work so a retry or
    # interruption cannot duplicate the parent summarization request.
    caller=SequenceCaller(repo,spec,view.counter,provider)
    ready=[]; by_id={n['node_id']:n for n in plan['nodes']}
    from .hierarchy import enabled, latest_nodes, latest_incremental_nodes
    hierarchical = enabled(repo)
    history=[{'id':n['id'],'summary':n['summary']} for n in repo.manifest['inputs'].get('history_nodes',[])]
    if hierarchical:
        history = [{'id': node['id'], 'summary': node['summary']}
                   for node in latest_nodes(repo) + latest_incremental_nodes(repo)
                   if node['level'] == 1]
    commits=dict((c['source_id'],ident) for ident,c in repo.all_commits())
    for root_id in plan['roots']:
        root=by_id[root_id]; previous=[]
        built_children = None

        def build_children(navigation_history):
            """Materialize this root's L2 blocks in reading order, once."""
            nonlocal built_children, previous
            if built_children is not None:
                return built_children
            built_children = []
            for child_id in root['children']:
                child=by_id[child_id]
                if child['state']=='open' and not force_open: continue
                pair_ids=child['ordered_source_ids']
                pairs=view.read_chapters(pair_ids)
                pair_hash=digest(pairs)
                candidates=[(i,n) for i,n in repo.store.objects('sequence-node') if n['node_id']==child_id
                            and n['pair_hash']==pair_hash and n['level']==2
                            and n.get('reference_profile', 'legacy') == spec.reference_profile]
                if candidates:
                    ident,node=candidates[0]; node_is_valid(repo,node)
                else:
                    messages,refs=joint_messages(pairs,navigation_history,previous,
                                                 reference_profile=spec.reference_profile)
                    result=recovery.run(child['revision_id']+':'+pair_hash,messages,parser(refs),
                        parser_version=STABLE_PARSER_VERSION if spec.reference_profile==STABLE else PARSER_VERSION,
                        max_tokens=spec.roles['indexer'].requested_output_tokens)
                    records=[]
                    names={r['id']:child_id+':'+r['id'] for r in result['records'] if r.get('id')}
                    for r in result['records']:
                        fields={k:list(v) for k,v in r['fields'].items()}
                        if r['type']=='TEMPORAL':
                            for key in ('from_event','to_event'): fields[key]=[names[x] for x in fields[key]]
                        records.append({**r,'id':names.get(r.get('id')),'fields':fields,'node_id':child_id,
                            'authority':'provisional_rendering' if r['type']=='ENTITY' and fields.get('target_form') else 'candidate_unreviewed'})
                    node={'schema':'sequence-node-v1','run_id':repo.run_id,'node_id':child_id,'level':2,
                        'parent_id':root_id,'node_revision':child['revision_id'],'source_ids':pair_ids,
                        'start_position':child['start_position'],'end_position':child['end_position'],
                        'pair_hash':pair_hash,'commit_ids':[commits[c] for c in pair_ids],
                        'dependencies':[dependency(d) for p in pairs for d in [p['source']]+p['translation']],
                        'predecessor_node_ids':[p['revision_id'] for p in previous],
                        'history_ids':[n['id'] for n in repo.manifest['inputs'].get('history_nodes',[])],
                        'summary':result['summary'],'summary_length':summary_length_info(result['summary']),
                        'records':records,'quarantined':result['quarantined'],
                        'extraction_result_id':result['id'],'state':result['state'],'failure_reason':result['failure_reason'],
                        'references':refs,'semantic_status':'candidate_unreviewed',
                        'build_method':'direct','fallback_reason':None}
                    if spec.reference_profile == STABLE:
                        node['reference_profile'] = STABLE
                    ident=repo.store.put('sequence-node',node)
                item={'id':ident,**node}; ready.append(item); built_children.append(item)
                previous.append({'node_id':child_id,'revision_id':ident,'summary':node['summary'] or '【缺口；仍须保留全文】'})
            return built_children

        # A hierarchy parent is attempted against the complete committed
        # prefix first.  Only a local/remote context-capacity failure is
        # allowed to trigger the L2 extraction needed for a rollup.
        parent_node = None
        parent_navigation = list(history)
        if hierarchical:
            sealed = [by_id[cid] for cid in root['children']
                      if by_id[cid]['state'] == 'sealed' or force_open]
            if root['state'] == 'sealed' or force_open or len(root['children']) > spec.n_child:
                ids = [cid for child in sealed for cid in child['ordered_source_ids']]
                if ids:
                    prefix = {**root, 'ordered_source_ids': ids}
                    parent_node, failure = _materialize_parent_direct(
                        repo, spec, view, provider, prefix, parent_navigation)
                    if failure:
                        parent_node = _materialize_parent_rollup(
                            repo, view, caller, prefix, build_children(parent_navigation), failure)
                    ready.append(parent_node)

        children = build_children(parent_navigation)
        children=[n for n in ready if n['parent_id']==root_id]
        if children:
            rollup={'run_id':repo.run_id,'node_id':root_id,'source_ids':[c for n in children for c in n['source_ids']],
                'commit_ids':[c for n in children for c in n['commit_ids']],
                'children':[n['id'] for n in children],'records':[r for n in children for r in n['records']],
                'open_prefix':root['state']=='open','semantic_status':'candidate_unreviewed'}
            repo.store.put('sequence-parent-metadata',rollup)
            usable=[n for n in children if n.get('summary') and n['state'] in {'extracted','partial'}]
            # An open prefix or an extraction gap must not become a seemingly
            # complete L1. One-child roots already have the exact L2 overview.
            parent_ready=(len(usable)>=2 and len(usable)==len(root['children']) and
                          (root.get('state')=='sealed' or force_open))
            if not hierarchical and root.get('layout')=='summary_rollup' and parent_ready:
                from .context import _parent_overview
                summary,summary_id=_parent_overview(repo,caller,usable,root_id)
                repo.store.put('sequence-parent-overview',{
                    'id':'sequence-parent-overview:'+digest({'node_id':root_id,'summary_id':summary_id,
                        'source_ids':rollup['source_ids']}),
                    'node_id':root_id,'children':[n['id'] for n in children],
                    'source_ids':rollup['source_ids'],'commit_ids':rollup['commit_ids'],
                    'summary':summary,'summary_revision':summary_id,
                    'open_prefix':root['state']=='open','semantic_status':'candidate_unreviewed',
                    'layout':'summary_rollup'})
        if parent_node:
            history.append({'id': parent_node['id'], 'summary': parent_node['summary']})
    with repo.db:
        done={c for n in ready if n['state'] in {'extracted','partial'} for c in n['source_ids']}
        for eid,payload in repo.db.execute("SELECT id,payload FROM sequence_outbox WHERE status='pending'").fetchall():
            if json.loads(payload)['source_id'] in done:
                repo.db.execute("UPDATE sequence_outbox SET status='completed' WHERE id=?",(eid,))
    from .entities import update_entity_catalog
    entity_nodes = latest_nodes(repo) + latest_incremental_nodes(repo)
    update_entity_catalog(repo, spec, view, caller, entity_nodes)
    return ready
