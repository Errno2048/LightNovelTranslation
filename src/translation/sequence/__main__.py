"""Thin CLI over immutable branch manifests. plan/status/explain are offline."""
import argparse
from pathlib import Path
import json

from ..config import load_settings
from ..narrative import TokenCounter
from ..providers import HTTPProvider, MockProvider
from ..storage import read_json
from . import SequenceSpec, SequenceRepository, SequenceEngine, create_sequence, fork_from_commit


def main():
    parser=argparse.ArgumentParser(__doc__)
    parser.add_argument('command',choices=['create','plan','run','resume','status','checkpoint','materialize','fork','explain'])
    parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('--config',type=Path)
    parser.add_argument('--tokenizer',type=Path)
    parser.add_argument('--inputs',type=Path)
    parser.add_argument('--spec',type=Path)
    parser.add_argument('--branch',default='working')
    parser.add_argument('--stop-after',type=int)
    parser.add_argument('--source-id')
    parser.add_argument('--commit-id')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--seal-tail',action='store_true')
    args=parser.parse_args()
    counter=TokenCounter(args.tokenizer)
    if args.command=='create':
        if not args.inputs or not (args.spec or args.config): parser.error('create requires --inputs and --spec or --config')
        settings=load_settings(Path.cwd(),args.config)
        spec=SequenceSpec.from_dict(read_json(args.spec)) if args.spec else SequenceSpec.from_settings(settings)
        repo=create_sequence(args.db,spec,read_json(args.inputs),
            branch_name=args.branch,provider_identity=settings.generation.public(),counter_identity=counter.identity)
        result={'run_id':repo.run_id,'heads':repo.heads(),'targets':len(repo.chapters())}
    else:
        repo=SequenceRepository(args.db)
        if args.command=='status':
            result={'run_id':repo.run_id,'heads':repo.heads(),'chapters':repo.chapters(),'attempts':repo.store.attempts()}
        else:
            if args.command in {'run','resume','checkpoint','materialize'}:
                settings=load_settings(Path.cwd(),args.config)
                provider=HTTPProvider(settings.generation,max_requests=repo.manifest['spec']['max_requests'])
            else: provider=MockProvider()
            engine=SequenceEngine(repo,provider,counter)
            if args.command=='plan':
                result=engine.plan_next()
                if args.output:
                    from ..storage import write_json
                    write_json(args.output,result)
                    result={k:v for k,v in result.items() if k!='context'}
            elif args.command in {'run','resume'}: result=engine.advance_sequence(stop_after=args.stop_after)
            elif args.command=='checkpoint': result={'checkpoint_id':engine.checkpoint()}
            elif args.command=='materialize':
                nodes=engine.materialize_pending(seal_tail=args.seal_tail)
                result=[{k:n[k] for k in ('id','node_id','source_ids','state','failure_reason')} for n in nodes]
            elif args.command=='explain':
                if not args.source_id or not args.output: parser.error('explain requires --source-id and --output')
                result=engine.explain_context(args.source_id,args.output)
            else:
                if not args.output: parser.error('fork requires --output (new sqlite path)')
                child=fork_from_commit(engine,args.output,args.commit_id,branch_name=args.branch)
                result={'run_id':child.run_id,'database':str(args.output)}; child.close()
    repo.close()
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__': main()
