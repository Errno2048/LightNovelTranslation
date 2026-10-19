"""Explicitly inherit an exact valid prefix; never resume a stale suffix."""
from dataclasses import replace

from ..narrative.schemas import NarrativeError
from .store import create_sequence


def fork_from_commit(engine,path,commit_id,*,branch_name,overrides=None,provider_identity=None,counter_identity=None,
                     recovery_seeds=None):
    repo=engine.repo; commits=repo.commits()
    if commit_id is not None and commit_id not in dict(commits):
        raise NarrativeError('visibility_denied','fork point is not a branch commit')
    prefix=[]
    if commit_id:
        for ident,c in commits:
            repo.validate_commit(ident); prefix.append((ident,c))
            if ident==commit_id: break
    source_ids={c['source_id'] for _,c in prefix}
    old_inputs=repo.manifest['inputs']
    targets=[d for d in old_inputs['targets'] if d['chapter_id'] not in source_ids]
    if not targets: raise NarrativeError('invalid_record','fork has no remaining target')
    view=engine.view(targets[0]['chapter_id'])
    history=list(old_inputs.get('history_pairs',[]))
    for ident,c in prefix:
        history.append({'source':view.body(c['source_id']),'translation':[view.generated_document(ident)],
            'translation_status':'generated_working' if not c.get('acceptance_id') else 'generated_accepted',
            'inherited_commit_id':ident})
    from .context import _latest_nodes, _workset
    eligible=_latest_nodes(repo,source_ids)
    inherited_workset=_workset(repo,eligible)
    candidates=inherited_workset['candidates']
    protocol_changed=(overrides or {}).get('reference_profile', engine.spec.reference_profile)!=engine.spec.reference_profile
    gaps=inherited_workset['extraction_gaps']
    if protocol_changed:
        # Do not carry numerically guessed legacy citations into a new registry.
        # Exact inherited prose is still available for rebuilding its indexes.
        candidates=[]
        gaps=gaps+[{'id':'fork-reference-protocol-change','source_ids':sorted(source_ids),
                    'reason':'reference_protocol_changed','authority':'requires_reextraction'}]
    inputs={**old_inputs,'targets':targets,'history_pairs':history,'inherited_candidates':candidates,
        'inherited_extraction_gaps':gaps,
        'inherited_commits':old_inputs.get('inherited_commits',[])+[{'id':i,'commit':c} for i,c in prefix],
        'fork_origin':{'run_id':repo.run_id,'commit_id':commit_id,'suffix_requires_new_generation':True}}
    if recovery_seeds:
        if set(recovery_seeds) - {d['chapter_id'] for d in targets}:
            raise NarrativeError('invalid_record', 'recovery seed must belong to an uncommitted fork target')
        inputs['layout_recovery_seeds'] = recovery_seeds
    spec=replace(engine.spec,**(overrides or {}))
    return create_sequence(path,spec,inputs,branch_name=branch_name,provider_identity=provider_identity or repo.manifest['provider'],
                           counter_identity=counter_identity or engine.counter.identity)
