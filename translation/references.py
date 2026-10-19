"""One model-visible chapter/paragraph namespace, backed by exact documents.

Order, physical crawler IDs and revision IDs remain storage concerns. Never
recover a citation by adding/subtracting an offset from its printed number.
"""
import json
import re

from .storage import digest

STABLE = 'stable-v1'
LEGACY = 'legacy'
PARAGRAPH_PATTERN = r'C[A-Za-z0-9_]+\.[JZ]\d+'


def chapter_ref(document):
    key = document['match_key']
    match = re.fullmatch(r'main:(\d+)', key)
    if match:
        return 'C' + format(int(match[1]), '04d')
    # Extras and other catalog namespaces must not collide with main chapters.
    return 'CX' + digest({'match_key': key})[:12].upper()


def pair_rows(pair):
    """Build an explicit registry; Chinese split documents share one sequence."""
    source = pair['source']
    cid = chapter_ref(source)
    rows, refs = [], {}
    for language, tag, docs in [('ja', 'J', [source]), ('zh', 'Z', pair['translation'])]:
        ordinal = 0
        for doc in docs:
            for pid, text in zip(doc['paragraph_ids'], doc['paragraph_texts']):
                if not text.strip():
                    continue
                ordinal += 1
                alias = f'{cid}.{tag}{ordinal:03d}'
                binding = {'language': language, 'chapter': source['order'],
                           'chapter_ref': cid, 'chapter_id': doc['chapter_id'],
                           'paragraph_id': pid, 'revision_id': doc['revision_id'],
                           'text': text, 'reference_profile': STABLE}
                if alias in refs:
                    raise ValueError('duplicate canonical paragraph alias')
                refs[alias] = binding
                rows.append({'id': pid, 'alias': alias, 'text': text,
                             'chapter_id': doc['chapter_id'], 'language': language})
    return rows, refs


def prompt_projection(value, documents=(), *, scrub_inline=True):
    """Remove competing internal numbering and old citation registries.

    This is presentation only. Stored summaries and provenance are unchanged.
    Unknown legacy citations are omitted, never reinterpreted as new evidence.
    """
    documents = list(documents)
    aliases = {d['chapter_id']: chapter_ref(d) for d in documents}
    aliases.update({d['match_key']: chapter_ref(d) for d in documents})
    omitted = {'references', 'evidence', 'original_citations', 'original_citation_fields',
               'start_position', 'end_position', 'order', 'disclosure_positions',
               'disclosed_at', 'dependencies', 'paragraph_ids', 'paragraph_id'}

    def project(item):
        if isinstance(item, dict):
            result = {}
            for key, content in item.items():
                if key in omitted:
                    continue
                if scrub_inline and key=='id' and re.fullmatch(PARAGRAPH_PATTERN,str(item.get('alias',''))):
                    continue  # The short alias already identifies this paragraph.
                if key in {'source_text','target_text','replacement'} or (
                        key in {'text','quote','raw','context'} and 'alias' in item):
                    result[key] = content  # Never edit actual manuscript extracts.
                    continue
                if key in {'ja', 'zh'} and isinstance(content, list):
                    kept = [r for r in content if isinstance(r, str) and
                            re.fullmatch(PARAGRAPH_PATTERN, r)]
                    if kept:
                        result[key] = kept
                    if len(kept) != len(content):
                        result['legacy_citations_omitted'] = True
                    continue
                result[key] = project(content)
            return result
        if isinstance(item, list):
            return [project(x) for x in item]
        if isinstance(item, str):
            if item in aliases:
                return aliases[item]
            if re.fullmatch(r'main:\d+', item):
                return chapter_ref({'match_key':item})
            if item.lstrip().startswith(('{', '[')):
                try:
                    return json.dumps(project(json.loads(item)), ensure_ascii=False,
                                      separators=(',', ':'))
                except (ValueError, TypeError):
                    pass
            if scrub_inline:
                if item.startswith(('ja:syosetu:', 'zh:linovelib:', 'generated:sequence:')):
                    return 'UNAVAILABLE:' + digest(item)[:12]
                item = re.sub(r'(?:ja|zh):main:\d+(?::\d+)?(?:@\d+(?:-\d+)?)?',
                              '〔旧定位已省略〕', item)
        return item

    return project(value)


def input_documents(inputs):
    docs = list(inputs['targets'])
    docs += [d for p in inputs.get('history_pairs', []) for d in [p['source']]+p['translation']]
    docs += [d for n in inputs.get('history_nodes', []) for d in n['dependencies']]
    return docs


def reference_task(text):
    """Adapt protocol instructions only; never apply replacements to prose."""
    for before, after in [
        ('R/L/S/T标签', '规范段落ID'), ('S/T标签', '规范段落ID'),
        ('S标签', '日文段落ID'), ('T标签', '中文段落ID'),
        ('R标签', '历史段落ID'), ('E标签', '段落ID'),
        ('S-demo', 'CDEMO.J001'), ('T-demo', 'CDEMO.Z001')]:
        text = text.replace(before, after)
    return text


def reference_examples(messages, stable):
    return ([{**m, 'content': reference_task(m['content'])} for m in messages]
            if stable else messages)
