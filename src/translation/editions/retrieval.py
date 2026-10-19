"""Cheap, bounded prose retrieval. Scores nominate evidence; they prove no facts."""
import re


def query_terms(text):
    """Chinese/Japanese surface cues, useful even when native names differ."""
    words = set(re.findall(r'[A-Za-z][A-Za-z0-9_-]{2,}|[ァ-ヺー]{3,}', text))
    for run in re.findall(r'[\u3400-\u9fff]{2,}', text):
        words.update(run[i:i+2] for i in range(len(run)-1))
    return words


def row_score(text, terms):
    return sum(min(text.count(term), 3) for term in terms if term in text)


def discover_forms(target):
    """Heuristic candidates remain unverified; ordinary loanwords may occur."""
    # finditer preserves both the Katakana match and the bracket capture.
    forms = [m.group(1) or m.group(0) for m in re.finditer(
        r'[ァ-ヺ][ァ-ヺー・･·]{2,}|【([^【】\n]{1,40})】', target['text'])]
    return list(dict.fromkeys(forms))


def window_rows(rows, hits, radius=2):
    keep = {i for hit in hits for i in range(max(0, hit-radius), min(len(rows), hit+radius+1))}
    return [row for i, row in enumerate(rows) if i in keep]


def reference_candidates(pipeline, target_id, associations=(), *, explicit_ids=(), terms=()):
    """Search all supplied Chinese prose, without assuming cross-edition order."""
    archive = pipeline.archive
    priorities = {}
    node_for = {}
    for artifact in associations:
        if artifact.get('status') != 'complete':
            continue
        for pair in artifact.get('result', {}).get('associations', []):
            source = pipeline.nodes[pair['source_node']]
            if target_id not in source['entry_ids'] or pair.get('relation') not in {'event_overlap', 'related_context'}:
                continue
            node = pipeline.nodes[pair['reference_node']]
            for eid in node['entry_ids']:
                priorities[eid] = min(priorities.get(eid, 9), 1 if pair['relation'] == 'event_overlap' else 2)
                node_for[eid] = node['node_id']
    explicit = set(explicit_ids)
    if any(eid not in archive.entries or archive.entries[eid]['language'] != 'zh' for eid in explicit):
        raise ValueError('explicit style references must be supplied Chinese chapter IDs')
    candidates = []
    for entry in archive.entries.values():
        if entry['language'] != 'zh' or not entry['kind'].startswith('narrative') or entry.get('metadata', {}).get('extra'):
            continue
        doc = archive.read(entry['entry_id'])
        if not doc['rows']:
            continue
        scores = [row_score(row['text'], terms) for row in doc['rows']]
        score = sum(sorted(scores, reverse=True)[:3])
        eid = doc['entry_id']
        priority = 0 if eid in explicit else priorities.get(eid, 3 if score else 4)
        reason = ('explicit_style_sample' if eid in explicit else
                  'associated_block' if eid in priorities else
                  'prose_term_retrieval' if score else 'supplied_edition_style_fallback')
        candidates.append({'document': doc, 'priority': priority, 'score': score,
                           'row_scores': scores, 'node_id': node_for.get(eid), 'reason': reason})
    return sorted(candidates, key=lambda c: (c['priority'], -c['score'],
                  c['document']['edition_id'], c['document']['edition_order']))


def excerpt(rows, scores=(), *, limit=48, radius=2):
    """Keep scored windows, or a contiguous opening sample when cues are absent."""
    hits = sorted((i for i, s in enumerate(scores) if s > 0), key=lambda i: (-scores[i], i))
    if not hits:
        return rows[:limit]
    chosen = set()
    for hit in hits:
        window = set(range(max(0, hit-radius), min(len(rows), hit+radius+1)))
        if len(chosen | window) <= limit:
            chosen.update(window)
    return [r for i, r in enumerate(rows) if i in chosen] or rows[hits[0]:hits[0]+1]
