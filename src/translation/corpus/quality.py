"""Conservative structural scan; signals require review, no semantic deletion."""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from ..text import zero_width_counts
from . import load_chapters
from .sections import normalized
from ..storage import read_json, read_jsonl, write_json, write_jsonl


def apply_integrity_holds(base: Path) -> dict:
    """Keep unresolved deterministic signals out of examples/evaluation/KB."""
    chapters = load_chapters(base / 'corpus')
    flags = defaultdict(list)
    for issue in read_jsonl(base / 'corpus/corpus_issues.jsonl'):
        if issue['type'] == 'adjacent_repeated_paragraph':
            flags[issue['chapter_id']].append(issue)
    holds = []
    for chapter in chapters:
        if chapter['chapter_id'] in flags:
            chapter['metadata'].setdefault('reference_integrity', {}).update({
                'parallel_body_eligible': False, 'evaluation_eligible': False,
                'quality_holds': ['adjacent_repeated_paragraph_needs_independent_review']})
            holds.append({'chapter_id': chapter['chapter_id'], 'revision_id': chapter['revision_id'],
                          'key': chapter['match_key'], 'issues': flags[chapter['chapter_id']],
                          'action': 'exclude from examples, evaluation references and knowledge extraction; preserve all text'})
    write_jsonl(base / 'corpus/chapters.jsonl', chapters)
    manifest = read_json(base / 'corpus/corpus_manifest.json')
    manifest['integrity_hold_chapters'] = len(holds)
    write_json(base / 'corpus/corpus_manifest.json', manifest)
    write_json(base / 'corpus/quality_holds.json', holds)
    return {'held_chapters': len(holds), 'keys': [h['key'] for h in holds]}


def scan_quality(base: Path) -> dict:
    chapters = {c['chapter_id']: c for c in load_chapters(base / 'corpus')}
    rows, controls, kinds, conflicts = defaultdict(list), Counter(), Counter(), []
    with (base / 'corpus/paragraphs.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            row = json.loads(line)
            kinds[row['kind']] += 1
            controls.update(zero_width_counts(row['text']))
            for ruby in row['ruby']:
                for text in ruby.values():
                    controls.update(zero_width_counts(text))
            if row.get('section', {}).get('semantic_status') == 'known_numeric_conflict':
                conflicts.append(row['paragraph_id'])
            if chapters[row['chapter_id']]['language'] == 'zh' and row['kind'] == 'body':
                rows[row['chapter_id']].append(row)
    candidates = []
    for cid, paragraphs in rows.items():
        headings = [{'ordinal': p['ordinal'], 'text': p['text']} for p in paragraphs if re.match(r'^番外[篇編编]?\s*\d', p['text'])]
        controls_in_body = [{'ordinal': p['ordinal'], 'text': p['text']} for p in paragraphs if p['text'].strip() == '收藏']
        content = [p for p in paragraphs if normalized(p['text'])]
        texts = [normalized(p['text']) for p in content]
        seen, longest = defaultdict(list), None
        for i in range(max(0, len(texts)-4)):
            gram = tuple(texts[i:i+5])
            for j in seen[gram]:
                if i-j < 8:
                    continue
                n = 5
                while j+n < i and i+n < len(texts) and texts[j+n] == texts[i+n]:
                    n += 1
                characters = sum(len(t) for t in texts[i:i+n])
                if n >= 8 and characters >= 500 and (longest is None or n > longest['paragraphs']):
                    longest = {'first_ordinal': content[j]['ordinal'], 'second_ordinal': content[i]['ordinal'],
                               'paragraphs': n, 'characters': characters}
            seen[gram].append(i)
        if headings or longest or controls_in_body:
            candidates.append({'chapter_id': cid, 'key': chapters[cid]['match_key'],
                               'appendix_headings': headings, 'duplicate_block': longest, 'possible_site_controls': controls_in_body})
    source = {c['match_key']: c for c in chapters.values() if c['language'] == 'ja' and c['kind'] == 'narrative'}
    target = {c['match_key']: c for c in chapters.values() if c['language'] == 'zh' and c['kind'] == 'narrative'}
    result = {'version': 'structural-scan-v1', 'zero_width_controls': dict(controls),
              'paragraph_kinds': dict(kinds), 'unresolved_body_structural_candidates': candidates,
              'known_numeric_conflict_paragraphs': conflicts,
              'sequence_key_pairs': len(set(source) & set(target)), 'source_only_keys': sorted(set(source)-set(target)),
              'target_only_keys': sorted(set(target)-set(source)),
              'held_chapter_keys': [c['match_key'] for c in chapters.values() if c.get('metadata', {}).get('reference_integrity', {}).get('parallel_body_eligible') is False],
              'limitation': 'Exact duplicate blocks and explicit appendix headings only. An empty candidate list does not prove semantic completeness or literary fidelity.'}
    write_json(base / 'verification/structural_scan.json', result)
    return result
