"""Bounded name lookup against native reference prose, independent of extraction."""
from ..storage import digest, write_json
from .archive import document_dependency
from .prompts import j
from .protocol import SCHEMA


PROFILE = 'edition-terminology-resolution-v1'
FIELDS = {'source_form', 'source_ref', 'target_form', 'reference_ref', 'status',
          'rendering_kind', 'description', 'reason'}
SYSTEM = '''你核对同一小说两个版本的名称用法，不建立平行译文，不解开匿名人物身份。
源侧原文和局部定义决定当前对象及称呼；参考中文只提供可能的已有译法。
源侧实体目录可能不全，中文摘要也可能漏抽；这次直接检查给出的中文原始正文。
区分对象身份与名字形式：爱称对爱称、全名对全名，不把昵称扩写为全名。
绰号的解释可能因版本改写不同。可以建议同一对象的称呼，但不能用文库解释覆盖Web解释。
对每个输入source_form恰好一条TERM。candidate须给实际source_ref/reference_ref，名称须逐字存在于相应正文。
没有证据用no_match，证据不足用ambiguous；不能根据作品记忆、字面相似或既有机器译文编译名。
description仅写当前源文局部定义中可知的角色/用途，不补真实身份和未来经历。
只输出平面记录，最后@@END：
@@TERM t1
source_form: 输入实际名称
source_ref: 输入实际日文标签
target_form: 中文正文实际称呼，缺失则省略
reference_ref: 包含称呼的中文标签，缺失则省略
status: candidate或ambiguous或no_match
rendering_kind: name或nickname或title或other
description: 当前源文局部角色/用途
reason: 具体依据及版本差异，不能确认时说明缺什么
@@END
不得输出格式示例值。'''


def parse_resolution(text, forms, source_rows, reference_rows):
    # A local schema avoids changing the shared extraction codec vocabulary.
    from ..review.protocol import parse_records
    parsed = parse_records(text, {'TERM'}, field_schema={**SCHEMA, 'TERM': FIELDS},
                           syntax_profile='p6-tolerant-v1')
    errors, items, seen = list(parsed['errors']), [], []
    for record in parsed['records']:
        row = record['fields']
        form = row.get('source_form')
        if form not in forms or form in seen:
            errors.append('unknown_or_duplicate_source_form:' + str(form))
            continue
        seen.append(form)
        source = source_rows.get(row.get('source_ref'))
        if not source or form not in source['text'] or row.get('status') not in {'candidate','ambiguous','no_match'} or not row.get('reason'):
            errors.append('invalid_term_source_or_status:' + form)
            continue
        if row['status'] == 'candidate':
            reference = reference_rows.get(row.get('reference_ref'))
            if not reference or not row.get('target_form') or row['target_form'] not in reference['text']:
                errors.append('target_form_not_in_reference_prose:' + form)
                continue
            if row.get('rendering_kind') not in {'name','nickname','title','other'} or not row.get('description'):
                errors.append('candidate_usage_description_required:' + form)
                continue
        items.append(row)
    if set(seen) != set(forms): errors.append('missing_source_form_decisions')
    return {'items': items, 'errors': errors, 'complete': not errors,
            'semantic_status': 'model_candidate_unreviewed', 'syntax_notes': parsed.get('notes', [])}


def resolve_target_terminology(pipeline, target_id, associations=(), entity_matches=(), *,
                               max_terms=8, input_tokens=16000, output_tokens=4096):
    """Explicit paid precompile; ordinary packet reads never invoke this."""
    target = pipeline.archive.read(target_id)
    source_rows = {r['ref']: r for r in target['rows']}
    entities = {e['form']: e for match in entity_matches for e in match['source_catalog']['entities']
                if e['form'] in target['text']}
    mappings = [row for match in entity_matches for row in match.get('mappings', [])]
    wanted, trace = [], []
    for form, entity in entities.items():
        candidates = [row for row in mappings if row.get('source_form') == form and row.get('status') == 'supported']
        nickname = any(word in entity.get('description', '') for word in ('爱称','绰号','昵称'))
        needs_lookup = not candidates or nickname and any(not row.get('reference_rendering') for row in candidates)
        trace.append({'source_form': form, 'source_extracted': True,
            'catalog_candidate_forms': list(dict.fromkeys(r['target_form'] for r in candidates)),
            'status': 'lookup_required' if needs_lookup else 'catalog_candidate_available'})
        if needs_lookup: wanted.append(form)
    selected = wanted[:max_terms]
    for row in trace:
        if row['source_form'] in wanted[max_terms:]: row['status'] = 'lookup_term_budget_omitted'
    base = {'schema': PROFILE, 'target_id': target_id, 'trace': trace, 'selected_forms': selected,
        'items': [], 'dependencies': [document_dependency(target)],
        'association_fingerprints': [a.get('fingerprint') for a in associations],
        'entity_match_fingerprints': [a.get('fingerprint') for a in entity_matches]}
    if not selected:
        base['status'] = 'not_needed'
        base['fingerprint'] = digest(base)
        return base

    # Select the chapter with actual overlapping-event evidence before other
    # members of that native reference L2. No cross-language ordinal guessing.
    documents = {}
    for artifact in associations:
        if artifact.get('status') != 'complete': continue
        for association in artifact.get('result', {}).get('associations', []):
            source_node = pipeline.nodes[association['source_node']]
            if association.get('relation') != 'event_overlap' or target_id not in source_node['entry_ids']: continue
            node = pipeline.nodes[association['reference_node']]
            summary = pipeline.read_summary(node['node_id'])
            events = {e['id']: e for e in summary['content']['events']} if summary else {}
            anchors = {events[pair['reference_event']]['evidence_ref'] for pair in [association]+association.get('additional_event_matches', [])
                if pair.get('reference_event') in events}
            for eid in node['entry_ids']:
                doc = pipeline.archive.read(eid)
                priority = 0 if any(r['ref'] in anchors for r in doc['rows']) else 1
                documents[eid] = (min(priority, documents.get(eid, (9,))[0]), doc)
    local_rows = []
    positions = [i for i, row in enumerate(target['rows']) if any(form in row['text'] for form in selected)]
    keep = {i for hit in positions for i in range(max(0, hit-2), min(len(target['rows']), hit+3))}
    local_rows = [row for i, row in enumerate(target['rows']) if i in keep]
    payload = {'forms': selected, 'source_local_prose': [{'ref':r['ref'],'text':r['text']} for r in local_rows],
               'reference_documents': []}
    deps = list(base['dependencies'])
    omitted = []
    for _, doc in sorted(documents.values(), key=lambda pair: (pair[0], pair[1]['edition_order'])):
        projected = {'edition_id':doc['edition_id'], 'entry_id':doc['entry_id'], 'title':doc['raw_title'],
                     'paragraphs':[{'ref':r['ref'],'text':r['text']} for r in doc['rows']]}
        candidate = {**payload, 'reference_documents':payload['reference_documents']+[projected]}
        if pipeline.counter.messages([{'role':'system','content':SYSTEM},{'role':'user','content':j(candidate)}]) > input_tokens:
            omitted.append(doc['entry_id'])
            continue
        payload = candidate
        deps.append(document_dependency(doc))
    base.update(dependencies=deps, omitted_reference_documents=omitted,
                search_scope=[d['entry_id'] for d in payload['reference_documents']])
    if not payload['reference_documents']:
        base['status'] = 'no_reference_scope'
        for row in trace:
            if row['source_form'] in selected: row['status'] = 'no_reference_scope'
    else:
        refs = {r['ref']:r for _, doc in documents.values() for r in doc['rows']
                if doc['entry_id'] in base['search_scope']}
        messages = [{'role':'system','content':SYSTEM},{'role':'user','content':j(payload)}]
        from ..providers import ProviderError
        try:
            outcome = pipeline.caller.structured('edition-terminology:'+target_id, messages,
                min(output_tokens, pipeline.capacity.max_output_tokens),
                lambda text: parse_resolution(text, selected, source_rows, refs))
        except ProviderError as exc:
            outcome = {'status':'failed', 'error':exc.kind,
                       'reason':'optional bounded terminology lookup unavailable; retain explicit gap'}
        base.update(status=outcome['status'], outcome=outcome)
        if outcome['status'] == 'complete':
            base['items'] = outcome['result']['items']
            for row in trace:
                decision = next((r for r in base['items'] if r['source_form'] == row['source_form']), None)
                if decision: row['status'] = decision['status']
        else:
            for row in trace:
                if row['source_form'] in selected: row['status'] = 'lookup_failed'
    base['fingerprint'] = digest(base)
    write_json(pipeline.folder / 'terminology' / (digest(target_id)[:24]+'.json'), base)
    return base
