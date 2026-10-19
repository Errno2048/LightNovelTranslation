"""Integrity checks for persisted, policy-scoped tool citations."""
from ..narrative.schemas import NarrativeError


def tool_evidence(view,scope,ids):
    rows,deps = {},{}
    for ident in ids:
        item = view.store.get(ident,'mvp-tool-result')
        if item['scope']!=scope:
            raise NarrativeError('visibility_denied','tool scope differs')
        view.archive.ensure_rows([d['chapter_id'] for d in item['dependencies']])
        for d in item['dependencies']:
            view.validate_dependency(d)
            deps[d['chapter_id']]=d
        for row in item['rows']:
            body=view.body(row['chapter_id'])
            if dict(zip(body['paragraph_ids'],body['paragraph_texts'])).get(row['id'])!=row['text']:
                raise NarrativeError('stale_evidence','tool paragraph changed')
            rows[row['alias']]=row
    return list(rows.values()),list(deps.values())
