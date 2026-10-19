"""P4a: small, reviewed chapter pairs -> monotone paragraph-range candidates.

Outputs are evidence for review, never approved examples or frozen knowledge.
Empty paragraphs and non-body sections retain their IDs in the exclusion list.
"""
from __future__ import annotations

import html
import json
import math
import re
import unicodedata
from decimal import Decimal
from collections import Counter, defaultdict
from pathlib import Path

from . import cached_embeddings, local_sequence_align
from ..corpus import load_chapters
from ..storage import atomic_text, digest, read_json, read_jsonl, write_json

VERSION = "paragraph-candidates-v2"


def normalized_numbers(text: str) -> Counter:
    """Conservative diagnostic equivalence, not a semantic acceptance rule.

    Single kanji digits in ordinary words and approximate quantities remain
    outside the guarantee. Units are retained separately by the caller.
    """
    text = unicodedata.normalize("NFKC", text)
    digits = {c: i for i, c in enumerate("零一二三四五六七八九")}
    digits.update({"〇": 0, "两": 2})
    units = {"十": 10, "百": 100, "千": 1000, "万": 10000, "億": 100000000, "亿": 100000000}
    values = []
    pattern = r"\d+(?:,\d{3})*(?:\.\d+)?[千万億亿]?|[零〇一二两三四五六七八九十百千万億亿]{2,}|(?<=第)[一二两三四五六七八九](?=[天日章回期年])"
    for match in re.finditer(pattern, text):
        value = match.group()
        if value[0].isdigit():
            multiplier = units.get(value[-1], 1)
            number = Decimal((value[:-1] if multiplier != 1 else value).replace(",", "")) * multiplier
        elif not any(c in units for c in value):
            number = Decimal("".join(str(digits[c]) for c in value))
        else:
            total, section, digit = 0, 0, 0
            for c in value:
                if c in digits:
                    digit = digits[c]
                elif units[c] < 10000:
                    section += (digit or 1) * units[c]
                    digit = 0
                else:
                    total += (section + digit or 1) * units[c]
                    section, digit = 0, 0
            number = Decimal(total + section + digit)
        values.append(str(number.normalize()))
    return Counter(values)


def _body(chapter, rows):
    if len(set(chapter['paragraph_ids'])) != len(chapter['paragraph_ids']) or [p['paragraph_id'] for p in rows] != chapter['paragraph_ids'] or any(p['revision_id'] != chapter['revision_id'] for p in rows):
        raise ValueError('Stale or incomplete paragraph records')
    if chapter.get('metadata', {}).get('reference_integrity', {}).get('order_status') == 'quarantined':
        raise ValueError('Quarantined reference cannot supply paragraph candidates')
    if chapter.get('metadata', {}).get('reference_integrity', {}).get('parallel_body_eligible') is False:
        raise ValueError('Reference has unresolved integrity holds')
    eligible, excluded = [], []
    for row in rows:
        if row['kind'] == 'body' and row['text'].strip() and row.get('section', {}).get('semantic_status') != 'known_numeric_conflict':
            eligible.append(row)
        else:
            excluded.append({'paragraph_id': row['paragraph_id'], 'kind': row['kind'],
                             'reason': 'empty' if not row['text'].strip() else 'nonparallel_section'})
    if not eligible:
        raise ValueError('No eligible body paragraphs')
    return eligible, excluded


def _unit(vector):
    norm = math.sqrt(sum(x*x for x in vector))
    if norm < 1e-12:
        raise ValueError('Zero semantic vector')
    return [x / norm for x in vector]


def align_paragraphs(source: dict, target: dict, source_rows: list, target_rows: list, embedder, cache: Path) -> dict:
    if source['language'] != 'ja' or target['language'] != 'zh':
        raise ValueError('Wrong paragraph alignment languages')
    left, excluded_left = _body(source, source_rows)
    right, excluded_right = _body(target, target_rows)
    if len(left) > 400 or len(right) > 400:
        raise ValueError('P4a window is limited to 400 body paragraphs per side; split into reviewed windows')
    # Chunk long paragraphs rather than silently truncating their semantic input.
    chunks, ownership = [], defaultdict(list)
    for row in left + right:
        for start in range(0, len(row['text']), 600):
            ownership[row['paragraph_id']].append(len(chunks))
            chunks.append(row['text'][start:start+600])
    vectors, usage, hits, batches = [], Counter(), 0, 0
    for start in range(0, len(chunks), 16):
        embedded = cached_embeddings(embedder, chunks[start:start+16], cache)
        vectors.extend(embedded['vectors'])
        usage.update({k: v for k, v in embedded['usage'].items() if isinstance(v, (int, float))})
        hits += embedded['cache_hit']
        batches += 1
    by_id = {}
    for row in left + right:
        indices = ownership[row['paragraph_id']]
        weights = [len(chunks[i]) for i in indices]
        by_id[row['paragraph_id']] = [sum(vectors[i][d]*w for i, w in zip(indices, weights)) / sum(weights)
                                      for d in range(len(vectors[0]))]
    block_vectors = {}
    for side in (left, right):
        for size in (1, 2):
            for i in range(len(side) - size + 1):
                block = side[i:i+size]
                weights = [len(p['text']) for p in block]
                block_vectors[tuple(p['paragraph_id'] for p in block)] = _unit([
                    sum(by_id[p['paragraph_id']][d]*w for p, w in zip(block, weights)) / sum(weights)
                    for d in range(len(vectors[0]))])
    chapter_ratio = sum(len(p['text']) for p in right) / sum(len(p['text']) for p in left)
    def features(a, b):
        va = block_vectors[tuple(p['paragraph_id'] for p in a)]
        vb = block_vectors[tuple(p['paragraph_id'] for p in b)]
        similarity = sum(x*y for x, y in zip(va, vb))
        ta, tb = '\n'.join(p['text'] for p in a), '\n'.join(p['text'] for p in b)
        ratio = len(tb) / max(1, len(ta))
        digits = [normalized_numbers(t) for t in (ta, tb)]
        digit_difference = digits[0] != digits[1]
        dialogue_difference = ta.lstrip().startswith(('「', '『', '"', '“')) != tb.lstrip().startswith(('「', '『', '"', '“'))
        length_deviation = abs(math.log(max(ratio, 1e-6)/chapter_ratio))
        return {'semantic_similarity': round(similarity, 6), 'length_ratio': round(ratio, 4),
                'number_difference': digit_difference, 'dialogue_shape_difference': dialogue_difference,
                'length_deviation': round(length_deviation, 4)}
    feature_cache = {}
    def score(a, b):
        key = tuple(p['paragraph_id'] for p in a), tuple(p['paragraph_id'] for p in b)
        f = features(a, b)
        feature_cache[key] = f
        return 4*(f['semantic_similarity']-.7)+.3-.18*f['length_deviation']-.22*f['number_difference']-.16*f['dialogue_shape_difference']
    path = local_sequence_align(left, right, score, max_group=2, gap_penalty=-.6)
    groups = []
    for step in path['steps']:
        a, b = [left[i] for i in step['source_indices']], [right[i] for i in step['target_indices']]
        ids = [p['paragraph_id'] for p in a], [p['paragraph_id'] for p in b]
        evidence = feature_cache.get((tuple(ids[0]), tuple(ids[1])), {})
        flags = []
        if not a:
            flags.append('unpaired_translation')
        if not b:
            flags.append('unpaired_source')
        if evidence.get('semantic_similarity', 1) < .72:
            flags.append('low_semantic_similarity')
        flags += [k for k in ('number_difference', 'dialogue_shape_difference') if evidence.get(k)]
        if evidence.get('length_deviation', 0) > 1:
            flags.append('length_outlier')
        groups.append({'range_id': 'paragraph:' + digest(ids)[:24], 'source_paragraph_ids': ids[0],
                       'translation_paragraph_ids': ids[1], 'relation': f'{len(a)}:{len(b)}',
                       'status': 'candidate', 'review_flags': flags, 'evidence': evidence})
    for name, side in (('source_paragraph_ids', left), ('translation_paragraph_ids', right)):
        if [pid for g in groups for pid in g[name]] != [p['paragraph_id'] for p in side]:
            raise ValueError('Paragraph alignment did not preserve ordered coverage')
    result = {'version': VERSION, 'source_chapter_id': source['chapter_id'], 'translation_chapter_id': target['chapter_id'],
              'source_revision_id': source['revision_id'], 'translation_revision_id': target['revision_id'],
              'source_dependencies': [source['chapter_id']], 'groups': groups,
              'excluded_source': excluded_left, 'excluded_translation': excluded_right,
              'usage': dict(usage), 'embedding_batches': batches, 'cache_hits': hits,
              'provider': embedder.config.public(),
              'ordered_id_coverage': True, 'semantic_gold_approved': False, 'calibrated_probability': False,
              'limitation': 'Heuristic candidates; gaps and feature differences are review signals, not proven translation errors. No knowledge or examples are approved.'}
    result['fingerprint'] = digest({k: v for k, v in result.items() if k not in {'usage', 'cache_hits'}})
    return result


def paragraph_candidates(base: Path, keys: list[str], embedder) -> dict:
    if not 1 <= len(keys) <= 3 or len(keys) != len(set(keys)):
        raise ValueError('P4a requires 1–3 unique chapter keys')
    chapters = load_chapters(base / 'corpus')
    lookup = {c['chapter_id']: c for c in chapters}
    sources = {c['match_key']: c for c in chapters if c['language'] == 'ja' and c['kind'] == 'narrative'}
    splits = read_json(base / 'evaluations/splits.json')
    selected = [sources[k] for k in keys]
    if any(c['chapter_id'] not in splits['development'] for c in selected):
        raise ValueError('Initial paragraph pilot is restricted to the frozen development set')
    confirmed = {s['chapter_id']: g for g in read_jsonl(base / 'alignment/groups.jsonl')
                 if g['status'] == 'confirmed' and g['relation'] == '1:1' for s in g['source_spans']}
    wanted, targets = set(), {}
    for source in selected:
        group = confirmed.get(source['chapter_id'])
        if not group:
            raise ValueError('Paragraph alignment requires reviewed chapter correspondence')
        target = lookup[group['translation_spans'][0]['chapter_id']]
        if group['source_spans'][0]['revision_id'] != source['revision_id'] or group['translation_spans'][0]['revision_id'] != target['revision_id']:
            raise ValueError('Stale chapter correspondence')
        if group['source_spans'][0]['paragraph_ids'] != source['paragraph_ids'] or group['translation_spans'][0]['paragraph_ids'] != target['paragraph_ids']:
            raise ValueError('Initial paragraph pilot requires reviewed full chapter identity spans')
        targets[source['chapter_id']] = target
        wanted.update((source['chapter_id'], target['chapter_id']))
    rows = defaultdict(list)
    with (base / 'corpus/paragraphs.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            row = json.loads(line)
            if row['chapter_id'] in wanted:
                rows[row['chapter_id']].append(row)
    reports = []
    for source in selected:
        target = targets[source['chapter_id']]
        result = align_paragraphs(source, target, rows[source['chapter_id']], rows[target['chapter_id']], embedder,
                                  base / 'alignment/paragraphs/embedding_cache')
        output = base / 'alignment/paragraphs' / source['match_key'].replace(':', '-')
        write_json(output / 'candidates.json', result)
        _review_html(result, rows[source['chapter_id']], rows[target['chapter_id']], output)
        reports.append({'key': source['match_key'], 'ranges': len(result['groups']),
                        'relations': dict(Counter(g['relation'] for g in result['groups'])),
                        'flagged_ranges': sum(bool(g['review_flags']) for g in result['groups']),
                        'usage': result['usage'], 'embedding_batches': result['embedding_batches'], 'cache_hits': result['cache_hits']})
    summary = {'version': VERSION, 'chapters': reports, 'development_only': True, 'semantic_gold_approved': False}
    write_json(base / 'alignment/paragraphs/pilot.json', summary)
    return summary


def _review_html(result, left, right, output):
    lookup = {p['paragraph_id']: p for p in left + right}
    parts = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>段落对齐候选</title><style>body{font:16px system-ui;margin:24px}article{display:flex;gap:24px;border-top:1px solid #ccc}section{flex:1;min-width:0}p{white-space:pre-wrap;line-height:1.7}small{color:#666}</style><body><p>候选对应；分数未校准。缺口和提示需复核，不自动进入知识库或示例。</p>']
    for group in result['groups']:
        parts.append('<h3>' + html.escape(group['relation'] + ' ' + ', '.join(group['review_flags'])) + '</h3><article>')
        for name in ('source_paragraph_ids', 'translation_paragraph_ids'):
            parts.append('<section>')
            for pid in group[name]:
                row = lookup[pid]
                parts.append(f"<p><small>p{row['ordinal']}</small><br>{html.escape(row['text'])}</p>")
            parts.append('</section>')
        parts.append('</article>')
    atomic_text(output / 'review.html', ''.join(parts) + '</body></html>')
