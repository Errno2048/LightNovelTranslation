"""Frozen review evidence shared by review, adjudication and repair."""
import json

from ..storage import digest
from ..narrative.schemas import NarrativeError
from ..narrative.service import dependency
from ..narrative.metadata import metadata_prompt
from .versions import read_version, source_segments, validate_additional_dependencies
from ..references import STABLE, pair_rows, chapter_ref, prompt_projection

KINDS = {'global_knowledge', 'few_shot', 'coarse_overview', 'child_detail',
         'far_block_overview', 'near_block_overview', 'near_block_detail', 'translation_metadata',
         'cross_edition_reference'}


def _prior_context(view, context_id):
    value = view.store.get(context_id, 'review-context')
    if value.get('fingerprint') != digest({k:v for k,v in value.items() if k != 'fingerprint'}):
        raise NarrativeError('stale_summary', 'historical context fingerprint changed')
    # Its former task source is ordinary prior history for this consumer.
    validate_additional_dependencies(view, value['dependencies'])
    if value['source']['order'] >= view.archive.chapters[view.policy.target_id]['order']:
        raise NarrativeError('visibility_denied', 'historical context must precede this target')
    return value


def compile_review_context(view, version_id, *, profile='hierarchical', near_source_ids=None,
                           prior_context_ids=(), projection_version='clean'):
    if profile not in {'near', 'hierarchical'}:
        raise NarrativeError('invalid_record', 'unknown review context profile')
    version = read_version(view, version_id)
    stable = version.get('reference_profile') == STABLE
    if projection_version not in {'initial','clean'}:
        raise NarrativeError('invalid_record','unknown context presentation protocol')
    base = view.store.get(version['context_id'], 'review-context')
    if near_source_ids is None:
        near_source_ids = [b['id'] for b in base['prompt_blocks'] if b['kind'] == 'near_chapter']
    if len(set(near_source_ids)) != len(near_source_ids) or len(set(prior_context_ids)) != len(prior_context_ids):
        raise NarrativeError('invalid_record', 'duplicate review context input')
    pairs = view.read_chapters(list(near_source_ids))
    documents, rows, dependencies = [], [], {}
    for i,pair in enumerate(pairs,1):
        registered, _ = pair_rows(pair) if stable else ([], {})
        for j,doc in enumerate([pair['source']]+pair['translation'],1):
            segments,_ = source_segments(doc)
            aliases = {r['id']:r['alias'] for r in registered if r['chapter_id']==doc['chapter_id']}
            for k,row in enumerate(segments,1):
                row.update(alias=aliases[row['id']] if stable else f'R{i}D{j}P{k:03d}', chapter_id=doc['chapter_id'],
                           language=doc['language'], semantic_status='unreviewed')
            rows.extend(segments)
            documents.append({'chapter_id':doc['chapter_id'], 'match_key':doc['match_key'],
                'title':doc['title'], 'language':doc['language'], 'rows':segments,
                'translation_status':pair['translation_status']})
            dep = dependency(view.body(doc['chapter_id']))
            dependencies[dep['chapter_id']] = dep
    external_packet = None
    if hasattr(view, 'repo'):
        external_packet = view.repo.manifest['inputs'].get('edition_reference_packets', {}).get(version['source_id'])
    if external_packet:
        from ..editions.sequence import validate_reference_packet, selected_reference_materials
        validate_reference_packet(external_packet, version['source_id'])
        selection = base.get('edition_reference_selection')
        materials = selected_reference_materials(external_packet, selection)
        # Native-edition paragraph IDs are evidence aliases, not canonical
        # source/translation pairs. Register them for citation validation only.
        external_rows = []
        for doc in materials['source_history'] + materials['style_documents']:
            language = 'ja' if doc.get('edition_id') == external_packet.get('source_edition_id') else 'zh'
            for row in doc.get('rows', []):
                external_rows.append({'id': row['paragraph_id'], 'alias': row['ref'], 'text': row['text'],
                    'chapter_id': row['entry_id'], 'language': language,
                    'semantic_status': 'web_source_history' if selection is not None and language == 'ja' else 'cross_edition_not_parallel',
                    'allowed_uses': ['source_history_evidence'] if selection is not None and language == 'ja' else ['style_reference']})
        for term in materials['terminology']:
            for key, language in (('source_usage', 'ja'), ('reference_usage', 'zh')):
                row = term.get(key)
                if row:
                    external_rows.append({'id': row['paragraph_id'], 'alias': row['ref'], 'text': row['text'],
                        'chapter_id': row['entry_id'], 'language': language,
                        'semantic_status': 'observed_usage_candidate', 'allowed_uses': ['terminology_candidate']})
        if selection is not None:
            # Keep one binding when an excerpt is also a term usage. Existing
            # frozen legacy review contexts retain their original projection.
            unique = {}
            for row in external_rows:
                old = unique.get(row['alias'])
                if old:
                    if any(old[k] != row[k] for k in ('id', 'chapter_id', 'language', 'text')):
                        raise NarrativeError('conflict', 'cross-edition evidence alias collision')
                    old['allowed_uses'] = sorted(set(old['allowed_uses'] + row['allowed_uses']))
                else:
                    unique[row['alias']] = row
            external_rows = list(unique.values())
        rows.extend(external_rows)
    blocks, catalog = [], []
    targeted=base.get('targeted_evidence',{})
    if targeted.get('payload'):
        from ..evidence.targeted import validate_history_rows
        registered=validate_history_rows(view,targeted)
        known={r['alias']:r for r in rows}
        for row in registered:
            if row['alias'] not in known:
                rows.append({**row,'semantic_status':'unreviewed'})
            elif any(known[row['alias']][k]!=row[k] for k in ('id','chapter_id','language','text')):
                raise NarrativeError('conflict','targeted evidence alias collides with near history')
        for dep in targeted['dependencies']:
            dependencies[dep['chapter_id']]=dep
        # Only this chapter's frozen packet is carried forward. Prior chapter
        # packets must not be re-injected as if selected for the current task.
        blocks.append({'kind':'targeted_evidence','id':targeted['fingerprint'],'text':targeted['text']})
    snapshots = [base]+[_prior_context(view,cid) for cid in prior_context_ids]
    match_text = version['source_segments'] and '\n'.join(r['text'] for r in version['source_segments']) or ''
    match_text += '\n'+'\n'.join(p['source']['text'] for p in pairs)
    if profile == 'hierarchical':
        seen = set()
        for snapshot in snapshots:
            root_ids = set()
            for dep in snapshot['dependencies']:
                if dep['chapter_id'] != version['source_id']:
                    if dep['chapter_id'] in dependencies and dependencies[dep['chapter_id']] != dep:
                        raise NarrativeError('conflict','review dependency versions differ')
                    dependencies[dep['chapter_id']] = dep
            for block in snapshot['prompt_blocks']:
                if block['kind'] not in KINDS or digest(block) in seen:
                    continue
                # Re-select historical metadata for the current source/near text.
                if block['kind'] == 'translation_metadata' and snapshot is not base:
                    continue
                seen.add(digest(block)); blocks.append(block)
            if snapshot is not base and snapshot.get('multilevel_metadata'):
                parents = snapshot['multilevel_metadata']['parents']
                entries = [e for p in parents for e in p['entries'] if any(f in match_text for f in e['source_forms'])]
                workset = {'selected':entries, 'fixed_expressions':[r for p in parents for r in p['fixed_expressions'] if r['source_form'] in match_text],
                    'rendering_variants':[], 'conflicts':[r for p in parents for r in p['conflicts']]}
                blocks.append({'kind':'translation_metadata','text':metadata_prompt(workset),
                               'origin_context':snapshot['fingerprint']})
            if snapshot.get('index_id'):
                index = view.store.get(snapshot['index_id'], 'multilevel-index')
                root_ids = {node['node_id'] for node in index['roots']}
                for node in index['roots']:
                    # Titles add navigation without duplicating every chapter ID twice.
                    headers = node['headers']
                    for h in headers:
                        source = view.archive.sources.get(h['match_key'])
                        if not source: raise NarrativeError('visibility_denied','unknown index header')
                        view.check(source)
                    catalog.append({'node_id':node['node_id'],'level':node['level'],
                        'chapter_range':[headers[0]['match_key'],headers[-1]['match_key']],
                        'title_hints':[h['title'] for h in headers[:2]+headers[-2:]],
                        'details_status':'explicit_lookup_required'})
            # Include detailed range summaries that translation's old compiler did not render.
            for selected in snapshot.get('selected_summaries',[]):
                artifact = view.store.get(selected['summary_id'], 'multilevel-summary')
                if artifact['node_id'] in root_ids or not artifact['content'].get('details'):
                    continue
                detail = {'kind':'child_ranges','id':selected['summary_id'],
                          'text':json.dumps(artifact['content']['details'],ensure_ascii=False)}
                if digest(detail) not in seen:
                    seen.add(digest(detail)); blocks.append(detail)
    if version.get('output_binding'):
        blocks.append({'kind':'translation_metadata', 'id':'source-bound-output:'+version_id,
            'text':'当前段落对应来自翻译模型显式填写的源段ID，只证明结构覆盖，未经语义确认。'
                   '请检查相邻段之间是否搬移了引语后的叙述、是否遗漏或重复，不能因同号或段数相同就判定正确。'})
    validate_additional_dependencies(view,list(dependencies.values()))
    data = {'schema':'review-evidence-context-v1','version_id':version_id,
        'source_id':version['source_id'],'policy':view.policy.public(), 'profile':profile,
        'base_context_id':version['context_id'],'near_source_ids':list(near_source_ids),
        'prior_context_ids':list(prior_context_ids),'blocks':blocks,'catalog':catalog,
        'documents':documents,'rows':rows,'dependencies':[dependencies[k] for k in sorted(dependencies)],
        'semantic_status':'historical_navigation_and_observed_usage_not_confirmed_facts'}
    if stable:
        data['reference_profile'] = STABLE
        if projection_version!='initial':
            data['projection_version'] = projection_version
        data['reference_documents'] = [{k:d[k] for k in ('chapter_id','match_key')}
            for d in view.archive.chapters.values() if d['chapter_id'] in
            {dep['chapter_id'] for dep in base['dependencies']} | {r['chapter_id'] for r in documents}]
    if external_packet:
        data['cross_edition_reference'] = {'target_id': version['source_id'],
            'packet_fingerprint': external_packet['fingerprint'],
            'reference_mode': 'cross_edition', 'use': 'style_and_observed_terminology_only'}
    return view.store.put('review-evidence-context',data)


def read_review_context(view, version_id, context_id):
    value = view.store.get(context_id,'review-evidence-context')
    version = read_version(view,version_id)
    if value['version_id'] != version_id or value['policy'] != view.policy.public() or value['base_context_id'] != version['context_id']:
        raise NarrativeError('conflict','review evidence belongs to another version/policy')
    validate_additional_dependencies(view,value['dependencies'])
    expected = compile_review_context(view,version_id,profile=value['profile'],
        near_source_ids=value['near_source_ids'],prior_context_ids=value['prior_context_ids'],
        projection_version=value.get('projection_version','initial'))
    if expected != context_id:
        raise NarrativeError('stale_summary','review context differs from frozen evidence')
    return value


def context_messages(value):
    instructions = ('【审校上下文使用规则】以下是允许的历史资料，不含目标章已有参考译文。'
        'R标签是可逐字引用的近章原文或历史译文；evidence_ref可用R标签。'
        '摘要及实体描述语义未审，不可覆盖本章原文。日文用于事实判断，历史中文用于核对已有译法；'
        '同名或译法冲突须保留适用范围。区分未发现问题与缺少证据。')
    messages = [{'role':'user','content':instructions}]
    stable = value.get('reference_profile') == STABLE
    project = lambda x: prompt_projection(x, value.get('reference_documents', value['documents']),
        scrub_inline=value.get('projection_version')=='clean') if stable else x
    if stable:
        messages[0]['content'] = ('【审校上下文使用规则】以下只含允许的历史资料，不含目标章参考译文。'
            '历史正文也使用C章节.J/Z段落ID，evidence_ref复制实际ID。日文用于事实判断，中文用于核对译法。'
            '导航摘要和候选知识不能覆盖正文；没有旧引用的摘要仅作导航。')
    if value.get('cross_edition_reference'):
        messages[0]['content'] += ('跨版本材料是独立文库版本，不与目标Web正文逐章或逐段对应；'
            '只能作为语言风格和有用译名的候选。文库事件、关系、时间和状态不能用作目标Web事实，冲突时以Web原文为准。')
    if value['blocks']:
        messages.append({'role':'user','content':'【分层前情与实体用法；保留来源和可信状态】\n'+json.dumps(project(value['blocks']),ensure_ascii=False)})
    if value['catalog']:
        messages.append({'role':'user','content':'【带标题的历史目录】\n'+json.dumps(project(value['catalog']),ensure_ascii=False)})
    if value['documents']:
        # Keep complete prose and citable labels; internal row IDs/offsets remain
        # in the frozen object and do not need repetition in every model role.
        documents = [{**{k:v for k,v in doc.items() if k != 'rows'},
                      'rows':[{'alias':r['alias'],'text':r['text']} for r in doc['rows']]}
                     for doc in value['documents']]
        messages.append({'role':'user','content':('【近章完整双语；规范段落ID可作证据】\n' if stable else
            '【近章完整双语；R标签可作证据】\n')+json.dumps(project(documents),ensure_ascii=False)})
    return messages
