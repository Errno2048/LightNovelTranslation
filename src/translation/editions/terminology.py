"""Bounded name lookup against native reference prose, independent of extraction."""
from ..storage import digest, write_json
from .archive import document_dependency
from .prompts import j
from .protocol import SCHEMA
from .retrieval import discover_forms, excerpt, query_terms, reference_candidates, window_rows


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
                           syntax_profile='tolerant-records')
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
                               max_terms=16, input_tokens=16000, output_tokens=4096,
                               max_documents=12, radius=2, discover=True, source_forms=()):
    """Explicit paid precompile; ordinary packet reads never invoke this."""
    target = pipeline.archive.read(target_id)
    source_rows = {r['ref']: r for r in target['rows']}
    entities = {e['form']: e for match in entity_matches for e in match['source_catalog']['entities']
                if e['form'] in target['text']}
    prior_entities, prior_summaries = [], []
    for node in pipeline.nodes.values():
        if (node['level'] != 2 or node['edition_id'] != target['edition_id'] or
                node['reading_range'][1] >= target['edition_order']):
            continue
        summary = pipeline.read_summary(node['node_id'])
        if summary:
            prior_entities.extend(summary['content']['entities'])
            prior_summaries.append(summary['fingerprint'])
    for entity in prior_entities:
        if entity['form'] in target['text']:
            entities.setdefault(entity['form'], entity)
    for form in source_forms:
        if not isinstance(form, str) or not form or form not in target['text']:
            raise ValueError('explicit source forms must occur in the current source chapter')
        entities.setdefault(form, {'form': form, 'origin': 'explicit'})
    if discover:
        for form in discover_forms(target):
            entities.setdefault(form, {'form': form, 'origin': 'surface_candidate'})
    mappings = [row for match in entity_matches for row in match.get('mappings', [])]
    wanted, trace = [], []
    ordered = sorted(entities, key=lambda form: (
        0 if form in source_forms else 1 if entities[form].get('kind') == 'person' else 2,
        -target['text'].count(form), -len(form), form))
    for form in ordered:
        entity = entities[form]
        candidates = [row for row in mappings if row.get('source_form') == form and row.get('status') == 'supported']
        nickname = any(word in entity.get('description', '') for word in ('爱称','绰号','昵称'))
        needs_lookup = not candidates or nickname and any(not row.get('reference_rendering') for row in candidates)
        trace.append({'source_form': form, 'source_extracted': 'origin' not in entity,
            'candidate_origin': entity.get('origin', 'entity_catalog'),
            'catalog_candidate_forms': list(dict.fromkeys(r['target_form'] for r in candidates)),
            'status': 'lookup_required' if needs_lookup else 'catalog_candidate_available'})
        if needs_lookup: wanted.append(form)
    selected = wanted[:max_terms]
    for row in trace:
        if row['source_form'] in wanted[max_terms:]: row['status'] = 'lookup_term_budget_omitted'
    base = {'schema': PROFILE, 'target_id': target_id, 'trace': trace, 'selected_forms': selected,
        'items': [], 'dependencies': [document_dependency(target)],
        'retrieval_profile': 'supplied-prose-windows-v2', 'corpus_fingerprint': pipeline.archive.fingerprint,
        'retrieval_source_summary_fingerprints':prior_summaries,
        'budget': {'input_tokens': input_tokens, 'output_tokens': output_tokens,
                   'max_terms': max_terms, 'max_documents': max_documents},
        'association_fingerprints': [a.get('fingerprint') for a in associations],
        'entity_match_fingerprints': [a.get('fingerprint') for a in entity_matches]}
    if not selected:
        base['status'] = 'not_needed'
        base['fingerprint'] = digest(base)
        write_json(pipeline.folder / 'terminology' / (digest(target_id)[:24]+'.json'), base)
        return base

    positions = [i for i, row in enumerate(target['rows']) if any(form in row['text'] for form in selected)]
    local_rows = window_rows(target['rows'], positions, radius)
    terms = query_terms('\n'.join(row['text'] for row in local_rows))
    # Earlier source descriptions bridge Japanese names to Chinese role words.
    # They nominate passages only; future/target-block descriptions are excluded.
    for entity in prior_entities:
        if any(form in entity['form'] for form in selected):
            terms.update(query_terms(entity.get('description', '')))
    terms.update(row['target_form'] for row in mappings if row.get('status') == 'supported')
    documents = reference_candidates(pipeline, target_id, associations, terms=terms)
    payload = {'forms': selected, 'source_local_prose': [{'ref':r['ref'],'text':r['text']} for r in local_rows],
               'reference_documents': []}
    deps = list(base['dependencies'])
    omitted, partial, refs = [], [], {}
    def fits(candidate):
        return pipeline.counter.messages([{'role':'system','content':SYSTEM},
                                          {'role':'user','content':j(candidate)}]) <= input_tokens
    for candidate_doc in documents[:max_documents]:
        doc = candidate_doc['document']
        full_cost = pipeline.counter.count(j([{'ref':r['ref'],'text':r['text']} for r in doc['rows']]))
        # Use available capacity for complete prose. Window only genuinely long
        # chapters, rather than dropping a useful name from a short chapter.
        rows = (doc['rows'] if full_cost <= input_tokens/max_documents else
                excerpt(doc['rows'], candidate_doc['row_scores'], limit=96, radius=radius))
        projected = {'edition_id':doc['edition_id'], 'entry_id':doc['entry_id'], 'title':doc['raw_title'],
                     'paragraphs':[{'ref':r['ref'],'text':r['text']} for r in rows]}
        candidate = {**payload, 'reference_documents':payload['reference_documents']+[projected]}
        while not fits(candidate) and len(rows) > 1:
            rows = excerpt(rows, [sum(term in r['text'] for term in terms) for r in rows],
                           limit=max(1, len(rows)//2), radius=radius)
            projected = {**projected, 'paragraphs':[{'ref':r['ref'],'text':r['text']} for r in rows]}
            candidate = {**payload, 'reference_documents':payload['reference_documents']+[projected]}
        if not fits(candidate):
            omitted.append(doc['entry_id'])
            continue
        payload = candidate
        deps.append(document_dependency(doc))
        refs.update({row['ref']: row for row in rows})
        if len(rows) < len(doc['rows']):
            partial.append({'entry_id':doc['entry_id'], 'included_paragraphs':len(rows),
                            'omitted_paragraphs':len(doc['rows'])-len(rows)})
    base.update(dependencies=deps, omitted_reference_documents=omitted,
                partial_reference_documents=partial,
                candidate_document_count=len(documents), search_exhaustive=False,
                document_limit_omitted=max(0, len(documents)-max_documents),
                search_scope=[d['entry_id'] for d in payload['reference_documents']],
                searched_reference_refs=sorted(refs))
    if not payload['reference_documents']:
        base['status'] = 'no_reference_scope'
        base['scope_failure_reason'] = ('no_supplied_chinese_prose' if not documents else
            'source_context_exceeds_lookup_budget' if not fits({**payload, 'reference_documents':[]}) else
            'reference_windows_exceed_lookup_budget')
        for row in trace:
            if row['source_form'] in selected: row['status'] = 'no_reference_scope'
    else:
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
