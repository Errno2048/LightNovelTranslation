"""Bounded, deterministic retrieval aids; never infer a bilingual alignment.

Katakana and existing ENTITY forms generate candidates, not authoritative names.
All excerpts retain their actual paragraph registry, including split translations.
"""
import json
import re
import unicodedata

from ..narrative.service import dependency
from ..references import pair_rows, chapter_ref, prompt_projection
from ..storage import digest


KATAKANA = re.compile(r'[ァ-ヺー]{2,}(?:[・･•·．.][ァ-ヺー]{2,})*')
SEPARATORS = '・･•·．. \t\r\n\u3000'
INSTRUCTION = ('【本章定向词条与局部证据；自动检索的未审候选】\n'
    '以下是资料，不是错误清单或硬性译法。日文用于核对对象、定义、材料、性质和行为，中文用于查找既有用法。'
    '历史双语只确认章级对应；不同语言的段落编号不表示逐段对齐。共享词形命中的中文也只是候选片段。'
    '请核对每个出现位置的名称、实体类型和指代，不只第一处。'
    '片假名可能有多个外语原词；描述相容仅支持一种解释，不能单凭相容就排除其他解释。'
    '不得补入正文没有说明的属性；证据不足保留不确定，必要时查阅历史正文。'
    '检索、词条或窗口被预算省略不等于作品没有相关信息。\n')


def normalize(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', text) if c not in SEPARATORS)


def _strings(value):
    return value if isinstance(value, list) else [value] if isinstance(value, str) else []


def message(packet):
    return INSTRUCTION + json.dumps(prompt_projection(packet), ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def _visible_row(row):
    return {k:row[k] for k in ('alias','language','text')}


def _windows(rows, indexes, radius=2):
    selected = {i for k in indexes for i in range(max(0, k-radius), min(len(rows), k+radius+1))}
    return [row for i, row in enumerate(rows) if i in selected]


def build_targeted_evidence(view, source, workset, *, max_tokens=12288, max_terms=12):
    """No generation, no target Chinese, no future documents, no guessed links.

    Chinese snippets use shared literal Katakana/Latin anchors or candidate card
    renderings. Without anchors we include the complete short Chinese chapter,
    or explicitly leave it for lookup. We never select its Japanese row ordinal.
    """
    counter = view.counter
    current, _ = pair_rows({'source':source, 'translation':[]})
    text = normalize(source['text'])
    candidates = {}
    for form in KATAKANA.findall(unicodedata.normalize('NFKC', source['text'])):
        key = normalize(form)
        candidates.setdefault(key, {'source_form':form, 'candidate_cards':[]})
    for record in workset.get('core', []) + workset.get('candidates', []):
        fields = record.get('fields', record)
        forms = _strings(fields.get('name', [])) + _strings(fields.get('source_forms', [])) + _strings(fields.get('source', []))
        for form in forms:
            key = normalize(form)
            if len(key) < 2 or key not in text:
                continue
            item = candidates.setdefault(key, {'source_form':form, 'candidate_cards':[]})
            card = {'id':record.get('id'), 'description':fields.get('description', []),
                    'target_forms':_strings(fields.get('target_form', fields.get('target', []))),
                    'semantic_status':'candidate_unreviewed'}
            if card not in item['candidate_cards']:
                item['candidate_cards'].append(card)
    if not candidates:
        return None
    # Read only visible Japanese first. Chinese is fetched only for selected hits.
    history = [(s['chapter_id'], normalize(view.body(s['chapter_id'])['text'])) for s in view.visible_sources()]
    hits = {key:[cid for cid, body in history if key in body] for key in candidates}
    ordered = sorted(candidates, key=lambda key:(
        any(c in candidates[key]['source_form'] for c in SEPARATORS.strip()),
        bool(hits[key]), bool(candidates[key]['candidate_cards']), -len(hits[key]), len(key)), reverse=True)
    payload = {'schema':'targeted-evidence-v1', 'selection':'katakana_and_existing_entity_forms',
        'authority':'candidate_unreviewed', 'terms':[], 'historical_excerpts':[],
        'local_source_windows':[], 'window_radius_nonempty_paragraphs':2,
        'omitted':{'terms':len(ordered), 'historical_chapters':0, 'local_rows':0}}
    fits = lambda: counter.count(message(payload)) + 32 <= max_tokens
    selected = []
    for key in ordered[:max_terms]:
        item = {**candidates[key], 'occurrences':[{'ref':r['alias'], 'count':normalize(r['text']).count(key)}
            for r in current if key in normalize(r['text'])],
            'historical_match_count':len(hits[key]),
            'historical_status':'available_for_lookup' if hits[key] else 'no_literal_hit_in_visible_frozen_history'}
        payload['terms'].append(item)
        payload['omitted']['terms'] -= 1
        if not fits():
            payload['terms'].pop(); payload['omitted']['terms'] += 1
            continue
        selected.append(key)
    if not selected:
        return None
    hit_positions=list(dict.fromkeys(i for key in selected for i,r in enumerate(current) if key in normalize(r['text'])))
    window=_windows(current,hit_positions)
    # Reserve space for actual local definitions before historical retrieval.
    # Exact hits precede neighbors; the final presentation keeps reading order.
    priority=hit_positions+[i for i,r in enumerate(current) if r in window and i not in hit_positions]
    local=[]
    for i in priority:
        local.append(current[i])
        payload['local_source_windows']=[_visible_row(r) for r in sorted(local,key=lambda r:r['alias'])]
        if counter.count(json.dumps(payload['local_source_windows'],ensure_ascii=False))>max_tokens//3 or not fits():
            local.pop()
    payload['local_source_windows']=[_visible_row(r) for r in sorted(local,key=lambda r:r['alias'])]
    payload['omitted']['local_rows']=len(window)-len(local)
    dependencies, registered, source_ids = {}, {}, []
    # Retrieve first mention and most recent mention, retaining scope/conflicts.
    for key in selected:
        ids = list(dict.fromkeys(hits[key][:1] + hits[key][-1:]))
        for cid in ids:
            pair = view.read_chapters([cid])[0]
            rows, _ = pair_rows(pair)
            ja = [r for r in rows if r['language']=='ja']
            zh = [r for r in rows if r['language']=='zh']
            ji=[i for i,r in enumerate(ja) if key in normalize(r['text'])]
            ji=list(dict.fromkeys(ji[:1]+ji[-1:]))
            window = _windows(ja,ji)
            anchor_text = '\n'.join(ja[i]['text'] for i in ji)
            anchors = {normalize(f) for f in KATAKANA.findall(unicodedata.normalize('NFKC', anchor_text)) if normalize(f)!=key}
            anchors.update(re.findall(r'[A-Za-z][A-Za-z0-9_-]{2,}', anchor_text))
            anchors.update(normalize(f) for c in candidates[key]['candidate_cards'] for f in c['target_forms'] if len(f)>=2)
            zi = [i for i,r in enumerate(zh) if any(a in normalize(r['text']) for a in anchors)]
            if zi:
                chosen = _windows(zh, zi)
                alignment = 'shared_literal_or_candidate_rendering_hits_not_paragraph_alignment'
            elif zh and counter.count('\n'.join(r['text'] for r in zh)) <= 2048:
                chosen = zh
                alignment = 'complete_short_chinese_chapter_no_paragraph_alignment'
            else:
                chosen = []
                alignment = 'no_safe_chinese_excerpt_use_chapter_lookup'
            shown_rows=window+chosen
            excerpt = {'source_form':candidates[key]['source_form'], 'lookup_source':chapter_ref(pair['source']),
                'translation_status':pair['translation_status'], 'alignment_status':alignment,
                'rows':[_visible_row(r) for r in shown_rows]}
            if counter.count(json.dumps(excerpt,ensure_ascii=False))>min(2048,max_tokens//3):
                # Remove surrounding prose before dropping the bilingual hits.
                shown_rows=[ja[i] for i in ji]+([zh[i] for i in zi] if zi else [])
                excerpt['rows']=[_visible_row(r) for r in shown_rows]
                excerpt['selection']='matching_paragraphs_only_context_omitted_by_budget'
                if not zi and chosen:
                    excerpt['alignment_status']='complete_chinese_chapter_omitted_by_budget_use_chapter_lookup'
            if counter.count(json.dumps(excerpt,ensure_ascii=False))>min(2048,max_tokens//3):
                shown_rows=[ja[i] for i in ji]
                excerpt['rows']=[_visible_row(r) for r in shown_rows]
                excerpt['alignment_status']='chinese_excerpt_omitted_by_budget_use_chapter_lookup'
            # Share already displayed paragraphs rather than repeat long bodies
            # for every candidate that occurred in the same historical chapter.
            excerpt['reused_refs']=[r['alias'] for r in shown_rows if r['alias'] in registered]
            excerpt['rows']=[_visible_row(r) for r in shown_rows if r['alias'] not in registered]
            payload['historical_excerpts'].append(excerpt)
            if not fits():
                # Preserve a source-only hit when the Chinese candidate is costly.
                shown_rows=[ja[i] for i in ji]
                excerpt['rows'] = [_visible_row(r) for r in shown_rows if r['alias'] not in registered]
                excerpt['reused_refs']=[r['alias'] for r in shown_rows if r['alias'] in registered]
                excerpt['alignment_status'] = 'chinese_excerpt_omitted_by_budget_use_chapter_lookup'
                if not fits():
                    payload['historical_excerpts'].pop()
                    payload['omitted']['historical_chapters'] += 1
                    continue
            source_ids.append(cid)
            registered.update({r['alias']:r for r in shown_rows})
            used = {r['chapter_id'] for r in shown_rows}
            for doc in [pair['source']]+pair['translation']:
                if doc['chapter_id'] in used:
                    dependencies[doc['chapter_id']] = dependency(doc)
    # Accounting fields may add a few tokens; remove optional duplicate windows.
    while not fits() and payload['local_source_windows']:
        payload['local_source_windows'].pop(); payload['omitted']['local_rows'] += 1
    if not fits():
        return None
    return {'schema':'targeted-evidence-v1', 'payload':payload, 'text':message(payload),
        'history_source_ids':list(dict.fromkeys(source_ids)), 'history_rows':list(registered.values()),
        'dependencies':list(dependencies.values()), 'estimated_tokens':counter.count(message(payload))+32,
        'fingerprint':digest(payload)}


def validate_history_rows(view, packet):
    """Bind extra review citations to exact permitted historical paragraphs."""
    available = {}
    for pair in view.read_chapters(packet['history_source_ids']):
        available.update({r['alias']:r for r in pair_rows(pair)[0]})
    from ..narrative.schemas import NarrativeError
    if packet['fingerprint'] != digest(packet['payload']) or packet['text'] != message(packet['payload']):
        raise NarrativeError('stale_summary', 'targeted evidence presentation changed')
    shown={r['alias']:r for excerpt in packet['payload']['historical_excerpts'] for r in excerpt['rows']}
    if shown!={r['alias']:_visible_row(r) for r in packet['history_rows']}:
        raise NarrativeError('stale_summary', 'targeted evidence registry differs from shown excerpts')
    for row in packet['history_rows']:
        if available.get(row['alias']) != row:
            raise NarrativeError('stale_summary', 'targeted evidence citation differs from actual history')
    return packet['history_rows']
