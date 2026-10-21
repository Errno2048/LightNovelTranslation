"""Bounded speculative pipeline: translate window n+1 while window n is reviewed.

Translation may consume the previous window's provisional snapshot. Reviews are
serialized in window order and always use accepted context. ``lookahead`` bounds
how many unreviewed windows may exist before the next translation starts.
"""
import queue
import threading
import time

from .batch import (_staged_snapshot, review_commit_window, translate_stage)


def _window_docs(job, start, size):
    targets = job.repo.manifest['inputs'].get('targets', [])
    return targets[start:start + size]


def _reuse_snapshot(job, docs):
    chapters = {chapter['source_id']: chapter for chapter in job.repo.chapters()}
    for doc in docs:
        chapter = chapters.get(doc['chapter_id'], {})
        if not chapter.get('version_id') or chapter.get('state') == 'committed':
            return None
    return _staged_snapshot(job, docs)


def run_pipeline(job, *, windows=3, size=30, translation_concurrency=None,
                 review_concurrency=None, lookahead=None, materialize=True,
                 reuse_staged=True):
    next_chapter = job.engine.next_chapter()
    if not next_chapter:
        return {'state': 'no_targets'}
    targets = job.repo.manifest['inputs'].get('targets', [])
    start = next((i for i, doc in enumerate(targets)
                  if doc['chapter_id'] == next_chapter['source_id']), None)
    if start is None:
        return {'state': 'no_targets'}
    window_docs = []
    for index in range(windows):
        docs = _window_docs(job, start + index * size, size)
        if docs:
            window_docs.append(docs)
    return run_pipeline_windows(
        job, window_docs, translation_concurrency=translation_concurrency,
        review_concurrency=review_concurrency, lookahead=lookahead,
        materialize=materialize, reuse_staged=reuse_staged)


def run_pipeline_windows(job, window_docs, *, translation_concurrency=None,
                         review_concurrency=None, lookahead=None, materialize=True,
                         reuse_staged=True):
    spec = job.engine.spec
    translation_concurrency = translation_concurrency or spec.translation_concurrency
    review_concurrency = review_concurrency or spec.review_concurrency
    lookahead = spec.provisional_lookahead if lookahead is None else lookahead
    if type(lookahead) is not int or lookahead < 0:
        raise ValueError('lookahead must be a non-negative integer')
    if not window_docs:
        return {'state': 'no_targets'}

    condition = threading.Condition()
    review_queue = queue.Queue()
    results = {}
    errors = []
    snapshots = {}
    state = {'translated': 0, 'reviewed': 0}

    def reviewer():
        while True:
            item = review_queue.get()
            if item is None:
                return
            index, docs = item
            try:
                result = review_commit_window(job, docs,
                                              review_concurrency=review_concurrency,
                                              materialize=materialize)
                results[index] = result
                if result.get('state') != 'complete':
                    errors.append({'window': index, 'stage': 'review',
                                   'state': result.get('state'),
                                   'committed': result.get('committed', [])})
            except Exception as exc:
                errors.append({'window': index, 'stage': 'review',
                               'error': repr(exc)})
            finally:
                with condition:
                    state['reviewed'] += 1
                    condition.notify_all()

    thread = threading.Thread(target=reviewer, daemon=True)
    thread.start()
    started = time.time()
    try:
        for index, docs in enumerate(window_docs):
            with condition:
                while (state['translated'] - state['reviewed'] > lookahead and
                       not errors):
                    condition.wait(timeout=30)
                if errors:
                    break
            provisional = snapshots.get(index - 1)
            with condition:
                previous_reviewed = (index - 1) in results
            if previous_reviewed:
                provisional = None
            try:
                snapshot = _reuse_snapshot(job, docs) if reuse_staged else None
                if snapshot is None:
                    stage = translate_stage(job, docs,
                                            concurrency=translation_concurrency,
                                            provisional=provisional)
                    snapshot = stage.get('snapshot')
                snapshots[index] = snapshot
            except Exception as exc:
                errors.append({'window': index, 'stage': 'translate',
                               'error': repr(exc)})
                break
            with condition:
                state['translated'] += 1
            review_queue.put((index, docs))
    finally:
        review_queue.put(None)
        thread.join()

    complete = (not errors and len(results) == len(window_docs) and
                all(result.get('state') == 'complete' for result in results.values()))
    return {'state': 'complete' if complete else 'partial',
            'windows': len(window_docs),
            'elapsed': round(time.time() - started, 1),
            'translated': state['translated'], 'reviewed': state['reviewed'],
            'results': results, 'errors': errors,
            'snapshot_generations': {index: snapshot['generation']
                                     for index, snapshot in snapshots.items() if snapshot}}
