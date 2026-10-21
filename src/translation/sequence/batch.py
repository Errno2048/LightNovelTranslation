"""Windowed glossary pre-extraction and parallel draft translation."""
from __future__ import annotations

import json
import re
import unicodedata

from ..narrative.schemas import NarrativeError
from ..references import pair_rows
from ..review.versions import create_version, read_version, source_segments
from ..storage import digest
from .context import compile_sequence_context
from .output import parse_output, recovery_feedback

try:
    from opencc import OpenCC
    _T2S = OpenCC('t2s')
except Exception:
    _T2S = None

KATAKANA = re.compile(r'[ァ-ヶー]{2,}')
BRACKETED = re.compile(r'【([^】]{2,30})】')
COMMON_KATAKANA = {
    'モンスター', 'プレゼント', 'ストーカー', 'チャンス', 'ダンス', 'ワルツ',
    'サー', 'クソ', 'ハチ', 'ニヤニヤ', 'バッチリ', 'キリッ', 'チラリ',
    'エネルギー', 'レベル', 'スキル', 'アイテム', 'パーティー', 'グループ',
    'チーム', 'ゲーム', 'アニメ', 'イメージ', 'デザート', 'メイド',
    'スタート', 'ストップ', 'オッケー', 'ハーフ', 'フル', 'タイプ', 'パターン',
}


def _window_targets(job, size):
    docs = job.repo.manifest['inputs'].get('targets', [])
    chapter = job.engine.next_chapter()
    if not chapter:
        return []
    start = next((i for i, doc in enumerate(docs)
                  if doc['chapter_id'] == chapter['source_id']), None)
    if start is None:
        return []
    return docs[start:start + size]


def _known_glossary(job):
    from .hierarchy import latest_incremental_nodes, latest_nodes
    known = {}

    def add(fields):
        names = [str(value).strip() for value in fields.get('name', []) or [] if str(value).strip()]
        targets = [str(value).strip() for value in fields.get('target_form', []) or [] if str(value).strip()]
        if not names or not targets:
            return
        target = targets[0]
        if _T2S is not None:
            target = _T2S.convert(target)
        for name in names:
            known.setdefault(name, target)

    for node in latest_nodes(job.repo) + latest_incremental_nodes(job.repo):
        for record in node.get('records', []):
            if record.get('type') == 'ENTITY':
                add(record.get('fields', {}))
    try:
        catalog = job.entity_catalog()
        for entity in catalog.get('entities', []) if catalog.get('status') == 'found' else []:
            add(entity.get('fields', {}))
    except Exception:
        pass
    catalog = job.workset_catalog()
    if catalog and catalog.get('catalog_id'):
        value = job.repo.store.get(catalog['catalog_id'])
        for entity in value.get('entities', []):
            add(entity.get('fields', {}))
    return known


def _normalize(value):
    return ' '.join(unicodedata.normalize('NFKC', value).split()).casefold()


def _candidates(window, known, history_pairs):
    known_forms = {_normalize(value) for value in known}
    counts = {}
    contexts = {}
    for doc in window:
        text = doc.get('title', '') + '\n' + doc.get('text', '')
        for match in list(KATAKANA.finditer(text)) + list(BRACKETED.finditer(text)):
            form = match.group(1) if match.lastindex else match.group(0)
            forms = [form]
            if match.lastindex:
                forms = [form] + KATAKANA.findall(form)
            for value in forms:
                value = value.strip()
                if (len(value) < 2 or _normalize(value) in known_forms or
                        value in COMMON_KATAKANA):
                    continue
                counts[value] = counts.get(value, 0) + 1
                contexts.setdefault(value, text[max(0, match.start() - 120):match.end() + 160])
    ordered = sorted(counts, key=lambda value: (-counts[value], -len(value), value))[:60]
    hints = {value: [] for value in ordered}
    for pair in history_pairs:
        try:
            rows, _ = pair_rows(pair)
        except Exception:
            continue
        japanese = [row for row in rows if row.get('language') == 'ja']
        chinese = [row for row in rows if row.get('language') == 'zh']
        for index, row in enumerate(japanese):
            text = row.get('text', '')
            for value in ordered:
                if len(hints[value]) >= 2 or value not in text:
                    continue
                translation = ' '.join(
                    row.get('text', '') for row in chinese[max(0, index - 2):index + 3])
                hints[value].append({'ja': text[:360], 'zh': translation[:600]})
    return [{'form': value, 'count': counts[value], 'context': contexts[value][:360],
             'history_hints': hints[value]}
            for value in ordered]


def _extract_glossary(job, window, known):
    candidates = _candidates(window, known, job.repo.manifest['inputs'].get('history_pairs', []))
    if not candidates:
        return {}
    caller = job.engine.caller
    payload = {
        'window': [{'title': doc.get('title'), 'order': doc.get('order')}
                   for doc in window],
        'known_terms': [{'source': source, 'translation': target}
                        for source, target in list(known.items())[:100]],
        'candidates': candidates,
    }
    messages = [
        {'role': 'system', 'content':
         '你是日译中轻小说术语整理员。只处理窗口内尚未有既定译名的新专词。'
         '已有术语不得改译。每个候选译名必须能在窗口日文中找到原表记；'
         '如果 history_hints 提供了对应的既有中文段落，必须优先沿用其中的译名，不得另造新译名；'
         '忽略普通外来语、拟声词和一般名词，只保留人物、地名、种族、组织、技能、物品等专词；'
         '不确定时不要给出译名。只输出 JSON：'
         '{"terms":[{"source_form":"...","translation":"...","category":"...",'
         '"evidence":"...","confidence":"high|medium|low"}]}。'},
        {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False, separators=(',', ':'))},
    ]
    key = digest({'protocol': 'window-glossary-v1', 'window': [d['chapter_id'] for d in window],
                  'known': sorted(known), 'candidates': [c['form'] for c in candidates]})
    raw = caller.call('window-glossary:' + key, 'summary', messages,
                      role='indexer', max_tokens=min(8192, job.engine.spec.roles['indexer'].requested_output_tokens))
    text = raw['generation']['text']
    try:
        start, end = text.find('{'), text.rfind('}')
        value = json.loads(text[start:end + 1]) if start >= 0 and end > start else {}
    except (ValueError, TypeError):
        value = {}
    window_text = '\n'.join(doc.get('title', '') + '\n' + doc.get('text', '') for doc in window)
    terms = []
    for row in value.get('terms', []) if isinstance(value.get('terms'), list) else []:
        if not isinstance(row, dict):
            continue
        source = str(row.get('source_form', '')).strip()
        target = str(row.get('translation', '')).strip()
        if not source or not target or source not in window_text:
            continue
        if source in COMMON_KATAKANA:
            continue
        if _normalize(source) in {_normalize(value) for value in known}:
            continue
        terms.append({'source_form': source, 'translation': target,
                      'category': str(row.get('category', '')), 'confidence': str(row.get('confidence', '')),
                      'evidence': str(row.get('evidence', ''))})
    seen = set()
    unique = []
    for term in terms:
        if term['source_form'] in seen:
            continue
        seen.add(term['source_form'])
        unique.append(term)
    job.repo.store.put('sequence-window-glossary', {
        'run_id': job.repo.run_id, 'key': key,
        'window_source_ids': [doc['chapter_id'] for doc in window],
        'terms': unique, 'generation_id': raw['id'],
        'candidate_count': len(candidates),
    })
    return {term['source_form']: term['translation'] for term in unique}


def _batch_contexts(job, window, glossary, provisional=None):
    checkpoint_id = job.repo.heads().get('checkpoint')
    if not checkpoint_id:
        raise RuntimeError('window translation requires an existing checkpoint')
    items = []
    for doc in window:
        view = job.engine.view(doc['chapter_id'])
        context = compile_sequence_context(job.repo, job.engine.spec, view,
                                           checkpoint_id=checkpoint_id)
        messages = list(context['messages'])
        if glossary:
            messages.insert(1, {'role': 'user', 'content':
                '【本窗口冻结术语；翻译时优先使用，未列出的新词保留不确定性】\n' +
                json.dumps(glossary, ensure_ascii=False, separators=(',', ':'))})
        if provisional:
            remaining = (context.get('budget') or {}).get('remaining_input_tokens', 0)
            message = _provisional_message(provisional, counter=job.engine.counter,
                                           max_tokens=max(0, remaining - 8192))
            if message:
                messages.insert(2 if glossary else 1, message)
        items.append({'doc': doc, 'view': view, 'context': context, 'messages': messages})
    return items


def _parallel_translate(job, items, concurrency, retry_failed=False):
    caller = job.engine.caller
    spec = job.engine.spec
    for item in items:
        rows, _ = source_segments(item['context']['source'],
                                  reference_profile=spec.reference_profile)
        item['rows'] = rows
        item['aliases'] = [row['alias'] for row in rows]

    def parse(item, raw):
        if raw.get('error') or not raw.get('generation'):
            return {'status': 'failed', 'error': raw.get('error')}
        text = raw['generation']['text']
        parsed = None
        if spec.output_profile == 'source-records-v1':
            parsed = parse_output(text, item['rows'])
            if not parsed['complete']:
                return {'status': 'incomplete', 'generation_id': raw['id'],
                        'raw_text': text, 'parsed': parsed,
                        'errors': parsed.get('errors', [])}
            text = parsed['text']
        if _T2S is not None:
            text = _T2S.convert(text)
        return {'status': 'complete', 'text': text,
                'generation_id': raw['id'], 'parsed': parsed}

    jobs = [{'task': item['doc']['chapter_id'] + ':window-draft',
             'kind': 'translation', 'messages': item['messages'],
             'role': 'translator',
             'max_tokens': spec.roles['translator'].requested_output_tokens}
            for item in items]
    raws = caller.call_many(jobs, workers=concurrency, retry_failed=retry_failed)
    translations = {}
    pending = []
    for item, raw in zip(items, raws):
        source_id = item['doc']['chapter_id']
        parsed = parse(item, raw)
        translations[source_id] = {key: value for key, value in parsed.items()
                                   if key not in {'parsed'}}
        if parsed.get('status') == 'incomplete':
            item['last_parsed'] = parsed.get('parsed')
            item['last_raw_text'] = parsed.get('raw_text', '')
            pending.append(item)
    for round_index in range(min(2, max(0, spec.max_attempts - 1))):
        if not pending:
            break
        jobs = []
        for item in pending:
            source_id = item['doc']['chapter_id']
            feedback = recovery_feedback(item['last_parsed'], item['aliases'])
            messages = list(item['messages']) + [
                {'role': 'assistant', 'content': item.get('last_raw_text', '')},
                {'role': 'user', 'content': feedback},
            ]
            jobs.append({'task': source_id + ':window-recovery:' + str(round_index + 1),
                         'kind': 'translation', 'messages': messages,
                         'role': 'translator',
                         'max_tokens': spec.roles['translator'].requested_output_tokens})
        raws = caller.call_many(jobs, workers=concurrency, retry_failed=retry_failed)
        next_pending = []
        for item, raw in zip(pending, raws):
            parsed = parse(item, raw)
            translations[item['doc']['chapter_id']] = {
                key: value for key, value in parsed.items() if key not in {'parsed'}}
            if parsed.get('status') == 'incomplete':
                item['last_parsed'] = parsed.get('parsed')
                item['last_raw_text'] = parsed.get('raw_text', '')
                next_pending.append(item)
        pending = next_pending
    job.repo.store.put('sequence-window-translation', {
        'run_id': job.repo.run_id,
        'window_source_ids': [item['doc']['chapter_id'] for item in items],
        'translations': translations,
    })
    return translations


PROVISIONAL_OVERLAY_CHARS = 600000


def _provisional_message(snapshot, *, counter=None, max_tokens=None):
    if not snapshot:
        return None
    parts = ['【上一窗口未审校译文（provisional，仅用于情节/语气衔接；'
             '专名以冻结 glossary 和已确认历史为准，不得当作已确认事实）】']
    for doc in snapshot.get('documents', []):
        parts.append('### ' + doc.get('title', '') + '\n' + doc.get('text', ''))
    content = '\n\n'.join(parts)
    if max_tokens is not None and counter is not None:
        limit = max(0, int(max_tokens))
        if limit < 4096:
            return None
        if counter.count(content) > limit:
            low, high = 0, len(content)
            while low < high:
                middle = (low + high) // 2
                if counter.count(content[middle:]) <= limit:
                    high = middle
                else:
                    low = middle + 1
            content = '【provisional overlay 前部已截断】\n' + content[low:]
    elif len(content) > PROVISIONAL_OVERLAY_CHARS:
        content = '【provisional overlay 前部已截断】\n' + content[-PROVISIONAL_OVERLAY_CHARS:]
    return {'role': 'user', 'content': content}


def _staged_snapshot(job, window):
    chapters = {c['source_id']: c for c in job.repo.chapters()}
    documents = []
    for doc in window:
        chapter = chapters.get(doc['chapter_id'], {})
        version_id = chapter.get('version_id')
        if not version_id or chapter.get('state') == 'committed':
            return None
        view = job.engine.view(doc['chapter_id'])
        version = read_version(view, version_id)
        documents.append({'source_id': doc['chapter_id'], 'title': doc.get('title', ''),
                          'text': version['text'], 'version_id': version_id})
    return {'schema': 'sequence-provisional-snapshot-v1', 'run_id': job.repo.run_id,
            'window': [doc['chapter_id'] for doc in window], 'documents': documents,
            'generation': digest({'window': [doc['chapter_id'] for doc in window],
                                  'versions': [doc['version_id'] for doc in documents]})}


def translate_stage(job, window, *, concurrency=None, provisional=None, retry_failed=False):
    """Translate one explicit window and leave versions staged for review."""
    spec = job.engine.spec
    concurrency = concurrency or spec.translation_concurrency
    known = _known_glossary(job)
    glossary = _extract_glossary(job, window, known)
    items = _batch_contexts(job, window, glossary, provisional)
    translations = _parallel_translate(job, items, concurrency, retry_failed=retry_failed)
    versions = {}
    for item in items:
        source_id = item['doc']['chapter_id']
        translated = translations.get(source_id, {})
        if translated.get('status') != 'complete':
            continue
        try:
            version_id = create_version(item['view'], item['context'], translated['text'],
                                        finish_reason='stop')
        except Exception:
            continue
        job.repo.stage(source_id, 'generated', version_id=version_id,
                       generation_id=translated.get('generation_id'))
        versions[source_id] = version_id
    snapshot = _staged_snapshot(job, window)
    if snapshot:
        job.repo.store.put('sequence-provisional-snapshot', snapshot)
    return {'window': [doc['chapter_id'] for doc in window], 'versions': versions,
            'translations': translations, 'glossary': glossary, 'snapshot': snapshot}


def review_commit_window(job, window, *, review_concurrency=None, materialize=True):
    """Review already staged versions for an explicit window, then commit in order."""
    spec = job.engine.spec
    review_concurrency = review_concurrency or spec.review_concurrency
    chapters = {chapter['source_id']: chapter for chapter in job.repo.chapters()}
    versions = {}
    translations = {}
    for doc in window:
        chapter = chapters.get(doc['chapter_id'], {})
        if chapter.get('version_id') and chapter.get('state') != 'committed':
            versions[doc['chapter_id']] = chapter['version_id']
            translations[doc['chapter_id']] = {'status': 'staged',
                                               'generation_id': chapter.get('generation_id')}
    review_entries = []
    review_ok = {}
    for doc in window:
        source_id = doc['chapter_id']
        version_id = versions.get(source_id)
        if not version_id:
            continue
        chapter = chapters.get(source_id, {})
        if chapter.get('state') == 'review_finished' and chapter.get('review'):
            review_ok[source_id] = chapter['review']
            continue
        view = job.engine.view(source_id)
        version = read_version(view, version_id)
        context = job.repo.store.get(version['context_id'], 'review-context')
        review_entries.append((source_id, context, version_id))
    results = job.engine.review_many(review_entries, workers=review_concurrency) \
        if review_entries else []
    review_fallback = {}
    for source_id, result in results:
        if source_id is None:
            continue
        severe = _confirmed_severities(job, result)
        if result.get('error') or severe is None or severe & {'critical', 'major'}:
            version_id = versions.get(source_id)
            job.repo.stage(source_id, 'generated' if version_id else 'planned',
                           version_id=version_id,
                           generation_id=translations.get(source_id, {}).get('generation_id')
                           if version_id else None,
                           review=None, prepared_id=None)
            review_fallback[source_id] = (result.get('error') or
                                          ('major_or_critical_findings' if severe else 'review_missing'))
        else:
            if result.get('confirmed'):
                result = {**result, 'minor_only': True}
            job.repo.stage(source_id, 'review_finished', review=result)
            review_ok[source_id] = result
    committed = []
    for doc in window:
        source_id = doc['chapter_id']
        chapter = next((c for c in job.repo.chapters() if c['source_id'] == source_id), None)
        if (chapter and chapter['state'] == 'review_finished' and
                chapter.get('review') and chapter.get('version_id')):
            try:
                job.engine.commit_reviewed(source_id)
                committed.append(source_id)
                continue
            except NarrativeError:
                pass
        if chapter and chapter['state'] == 'generated' and chapter.get('version_id'):
            try:
                job.engine.commit_with_revision(source_id)
                committed.append(source_id)
                continue
            except Exception:
                pass
        result = job.run(max_chapters=1, materialize=False)
        if not result.ok or result.committed_count == 0:
            if source_id in review_ok:
                job.repo.stage(source_id, 'planned', version_id=None,
                               generation_id=None, review=None, prepared_id=None)
                result = job.run(max_chapters=1, materialize=False)
            if not result.ok or result.committed_count == 0:
                break
        committed.append(source_id)
    if materialize and committed:
        job.engine.materialize_pending(seal_tail=True)
        job.reconcile_threads()
        next_chapter = job.engine.next_chapter()
        if next_chapter:
            from .hierarchy_context import build
            view = job.engine.view(next_chapter['source_id'])
            build(job.repo, spec, view, caller=None, reason='window-materialized',
                  fault=None, previous_budget=None)
    return {'state': 'complete' if committed == [d['chapter_id'] for d in window] else 'partial',
            'window': [d['chapter_id'] for d in window], 'committed': committed,
            'review_fallback': review_fallback, 'parallel_reviewed': len(review_ok)}


def _confirmed_severities(job, result):
    if not result.get('confirmed'):
        return set()
    if not result.get('report_id'):
        return None
    report = job.repo.store.get(result['report_id'], 'review-report')
    severities = {finding.get('id'): finding.get('severity')
                  for finding in report.get('findings', [])}
    if set(result['confirmed']) - set(severities):
        return None
    return {severities[finding_id] for finding_id in result['confirmed']}


def run_window(job, *, size=None, concurrency=None, review_concurrency=None,
               materialize=True, reuse_staged=False):
    """Pre-extract terms, translate a window concurrently, then review/commit in order."""
    spec = job.engine.spec
    size = size or max(2, spec.window_l2_blocks * spec.child_max_chapters)
    concurrency = concurrency or spec.translation_concurrency
    review_concurrency = review_concurrency or spec.review_concurrency
    window = _window_targets(job, size)
    if not window:
        return {'state': 'no_targets'}
    before_cost = job.cost_usage()
    chapters = {chapter['source_id']: chapter for chapter in job.repo.chapters()}
    versions = {}
    translations = {}
    missing = []
    for doc in window:
        chapter = chapters.get(doc['chapter_id'], {})
        if (reuse_staged and chapter.get('version_id') and
                chapter.get('state') != 'committed'):
            versions[doc['chapter_id']] = chapter['version_id']
            translations[doc['chapter_id']] = {
                'status': 'staged', 'generation_id': chapter.get('generation_id')}
        else:
            missing.append(doc)
    glossary = {}
    if missing:
        known = _known_glossary(job)
        glossary = _extract_glossary(job, missing, known)
        items = _batch_contexts(job, missing, glossary)
        items_by_id = {item['doc']['chapter_id']: item for item in items}
        translations.update(_parallel_translate(job, items, concurrency))
        for doc in missing:
            source_id = doc['chapter_id']
            translated = translations.get(source_id, {})
            if translated.get('status') != 'complete':
                continue
            item = items_by_id[source_id]
            try:
                vid = create_version(item['view'], item['context'], translated['text'],
                                     finish_reason='stop')
            except Exception:
                continue
            job.repo.stage(source_id, 'generated', version_id=vid,
                           generation_id=translated.get('generation_id'))
            versions[source_id] = vid
    review_entries = []
    review_ok = {}
    for doc in window:
        source_id = doc['chapter_id']
        vid = versions.get(source_id)
        if not vid:
            continue
        chapter = chapters.get(source_id, {})
        if (reuse_staged and chapter.get('state') == 'review_finished' and
                chapter.get('review')):
            review_ok[source_id] = chapter['review']
            continue
        view = job.engine.view(source_id)
        version = read_version(view, vid)
        context = job.repo.store.get(version['context_id'], 'review-context')
        review_entries.append((source_id, context, vid))
    review_results = job.engine.review_many(review_entries, workers=review_concurrency) \
        if review_entries else []
    review_fallback = {}
    for source_id, result in review_results:
        if source_id is None:
            continue
        translated = translations.get(source_id, {})
        severe = _confirmed_severities(job, result)
        if result.get('error') or severe is None or severe & {'critical', 'major'}:
            # Keep the draft; the live path will re-review or revise it serially.
            version_id = versions.get(source_id)
            job.repo.stage(source_id, 'generated' if version_id else 'planned',
                           version_id=version_id,
                           generation_id=translated.get('generation_id') if version_id else None,
                           review=None, prepared_id=None)
            review_fallback[source_id] = (result.get('error') or
                                          ('major_or_critical_findings' if severe else 'review_missing'))
        else:
            if result.get('confirmed'):
                result = {**result, 'minor_only': True}
            job.repo.stage(source_id, 'review_finished', review=result)
            review_ok[source_id] = result
    committed = []
    for doc in window:
        source_id = doc['chapter_id']
        chapter = next((c for c in job.repo.chapters() if c['source_id'] == source_id), None)
        if (chapter and chapter['state'] == 'review_finished' and
                chapter.get('review') and chapter.get('version_id')):
            try:
                job.engine.commit_reviewed(source_id)
                committed.append(source_id)
                continue
            except NarrativeError:
                pass
        if chapter and chapter['state'] == 'generated' and chapter.get('version_id'):
            try:
                job.engine.commit_with_revision(source_id)
                committed.append(source_id)
                continue
            except Exception:
                pass
        result = job.run(max_chapters=1, materialize=False)
        if not result.ok or result.committed_count == 0:
            if source_id in review_ok:
                job.repo.stage(source_id, 'planned', version_id=None,
                               generation_id=None, review=None, prepared_id=None)
                result = job.run(max_chapters=1, materialize=False)
            if not result.ok or result.committed_count == 0:
                break
        committed.append(source_id)
    if materialize and committed:
        job.engine.materialize_pending(seal_tail=True)
        job.reconcile_threads()
        next_chapter = job.engine.next_chapter()
        if next_chapter:
            from .hierarchy_context import build
            view = job.engine.view(next_chapter['source_id'])
            build(job.repo, spec, view, caller=None, reason='window-materialized',
                  fault=None, previous_budget=None)
    after_cost = job.cost_usage()
    return {'state': 'complete' if committed == [d['chapter_id'] for d in window] else 'partial',
            'window': [doc['chapter_id'] for doc in window],
            'glossary_terms': len(glossary), 'committed': committed,
            'translations': {key: value.get('status') for key, value in translations.items()},
            'parallel_reviewed': len(review_ok),
            'review_fallback': review_fallback,
            'cost_before': before_cost, 'cost_after': after_cost}
