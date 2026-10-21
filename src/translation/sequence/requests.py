"""Durable, budgeted generation shared by translation and compression."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace

from ..budget import (RoleBudget, plan_request, require_budget, observe_usage,
                      read_calibration, calibration_profile)
from ..narrative.schemas import NarrativeError
from ..providers import ProviderError
from ..schemas import GenerationRequest
from ..storage import digest


def sequence_limits(spec):
    kinds=('translation','mapping','review','adjudication','revision','repair','summary','analysis','compression')
    return {**{k+'_requests':spec.max_requests for k in kinds},'requests':spec.max_requests,
            'input_tokens':spec.max_input_tokens,'output_tokens':spec.max_output_tokens,
            'context_limit':spec.capacity.context_window_tokens,'safety_tokens':spec.capacity.safety_tokens}


class SequenceCaller:
    def __init__(self,repo,spec,counter,provider,*,fault=None):
        self.repo,self.spec,self.counter,self.provider=repo,spec,counter,provider
        self.store=repo.store; self.fault=fault
        self.identity=repo.manifest['provider']
        if getattr(getattr(provider,'config',None),'retries',0):
            raise NarrativeError('invalid_config','sequence ledger requires disabled provider automatic retries')

    def call(self,task,kind,messages,*,role='translator',max_tokens=None,task_family=None):
        role_budget=self.spec.roles[role]
        output=max_tokens or role_budget.requested_output_tokens
        logical=digest({'run':self.repo.run_id,'task':task_family or task,'kind':kind})
        # Requests may change after output truncation, but cannot reset the
        # logical task's attempt cap by changing a request hash.
        attempts=[a for _,a in self.store.objects('sequence-request') if a['logical']==logical]
        attempts.sort(key=lambda a:a['number'])
        retained_error=None
        for attempt in attempts:
            if attempt['task']!=task: continue
            raws=[(i,r) for i,r in self.store.objects('sequence-response') if r['request_id']==attempt['id']]
            if not raws:
                retained_error='unknown_remote_result'
                continue  # reservation remains charged; result unknown
            ident,raw=raws[0]
            retained_error=raw.get('error')
            if raw.get('generation') and raw['generation']['finish_reason']=='stop' and raw['generation']['text'].strip():
                if attempt['messages']!=messages:
                    raise NarrativeError('conflict','saved response belongs to a different logical task input')
                for ledger in self.store.attempts():
                    if ledger['identity']==attempt['id'] and ledger['status']=='running':
                        self.store.put('budget-calibration',observe_usage(attempt['budget'],raw['generation'].get('usage',{}),
                            read_calibration(self.store,attempt['budget']['profile'])))
                        self.store.finish(ledger['seq'],'completed',{'response_id':ident,'recovered_from_durable_raw':True,
                            'usage':raw['generation'].get('usage',{})})
                return {'id':ident,**raw}
            if raw.get('error')=='truncated': output=min(attempt['max_tokens']*2,self.spec.capacity.max_output_tokens)
        last_error=retained_error or 'attempts_exhausted'
        for number in range(len(attempts),self.spec.max_attempts):
            profile=calibration_profile(self.identity,self.counter,role)
            report=require_budget(plan_request(messages,self.spec.capacity,
                RoleBudget(output,min(role_budget.reasoning_reserve_tokens,output-1)),self.counter,
                profile=profile,calibration=read_calibration(self.store,profile)))
            request_data={'schema':'sequence-request-v1','logical':logical,'task':task,'kind':kind,
                'number':number,'messages':messages,'max_tokens':output,'provider':self.identity,'budget':report}
            request_id='sequence-request:'+digest(request_data)
            request_data['id']=request_id
            # Object ID excludes its own id; ledger identity uses an equally
            # immutable request binding, not the database object's hash.
            request_id=self.store.put('sequence-request',request_data)
            seq=self.store.reserve(request_data['id'],kind,report['safe_input_tokens'],output,sequence_limits(self.spec))
            self.store.put('sequence-reservation',{'request_id':request_id,'ledger_identity':request_data['id'],'seq':seq})
            try:
                if self.fault: self.fault('before_request')
                result=self.provider.generate(GenerationRequest(messages,max_tokens=output,temperature=0,task='p7_'+kind))
                raw={'request_id':request_data['id'],'request_object_id':request_id,'generation':asdict(result)}
                if result.finish_reason!='stop' or not result.text.strip():
                    raw['error']='truncated' if result.finish_reason=='length' else 'empty'
            except ProviderError as exc:
                raw={'request_id':request_data['id'],'request_object_id':request_id,'error':exc.kind,'details':exc.details}
            raw_id=self.store.put('sequence-response',raw)
            if self.fault: self.fault('after_response_saved')
            usage=raw.get('generation',{}).get('usage',raw.get('details',{}).get('usage',{}))
            if usage:
                self.store.put('budget-calibration',observe_usage(report,usage,read_calibration(self.store,profile)))
            last_error=raw.get('error')
            self.store.finish(seq,'unknown_remote_result' if last_error in {'network','timeout'} else 'failed' if last_error else 'completed',
                {'response_id':raw_id,'usage':usage,'error':last_error})
            if not last_error: return {'id':raw_id,**raw}
            if last_error=='truncated':
                if output>=self.spec.capacity.max_output_tokens: break
                output=min(output*2,self.spec.capacity.max_output_tokens)
            elif last_error not in {'network','timeout','server','rate_limit'}: break
        raise NarrativeError(last_error or 'request_failed','bounded sequence task failed; raw attempts retained')

    def call_many(self, jobs, *, workers=2):
        """Reserve/settle serially, run independent provider calls concurrently."""
        prepared=[]
        for job in jobs:
            task=job['task']; kind=job.get('kind','translation'); role=job.get('role','translator')
            messages=job['messages']; role_budget=self.spec.roles[role]
            output=job.get('max_tokens') or role_budget.requested_output_tokens
            logical=digest({'run':self.repo.run_id,'task':task,'kind':kind})
            profile=calibration_profile(self.identity,self.counter,role)
            report=require_budget(plan_request(messages,self.spec.capacity,
                RoleBudget(output,min(role_budget.reasoning_reserve_tokens,output-1)),self.counter,
                profile=profile,calibration=read_calibration(self.store,profile)))
            request_data={'schema':'sequence-request-v1','logical':logical,'task':task,'kind':kind,
                'number':0,'messages':messages,'max_tokens':output,'provider':self.identity,'budget':report}
            request_id=self.store.put('sequence-request',request_data)
            request_data['id']=request_id
            seq=self.store.reserve(request_data['id'],kind,report['safe_input_tokens'],output,
                                   sequence_limits(self.spec))
            self.store.put('sequence-reservation',{'request_id':request_id,
                'ledger_identity':request_data['id'],'seq':seq})
            prepared.append({'job':job,'task':task,'kind':kind,'role':role,'messages':messages,
                'output':output,'report':report,'profile':profile,'seq':seq,
                'request_id':request_data['id'],'request_object_id':request_id})
        def run_one(item):
            try:
                result=self.provider.generate(GenerationRequest(item['messages'],
                    max_tokens=item['output'],temperature=0,task='p7_'+item['kind']))
                return {'generation':asdict(result)}
            except ProviderError as exc:
                return {'error':exc.kind,'details':exc.details}
            except Exception as exc:
                return {'error':'provider','details':{'transport_error':type(exc).__name__}}
        with ThreadPoolExecutor(max_workers=max(1,workers)) as pool:
            raws=list(pool.map(run_one,prepared))
        results=[]
        for item,raw in zip(prepared,raws):
            raw={'request_id':item['request_id'],'request_object_id':item['request_object_id'],**raw}
            raw_id=self.store.put('sequence-response',raw)
            usage=raw.get('generation',{}).get('usage',raw.get('details',{}).get('usage',{}))
            if usage:
                self.store.put('budget-calibration',observe_usage(item['report'],usage,
                    read_calibration(self.store,item['profile'])))
            last_error=raw.get('error')
            self.store.finish(item['seq'],
                'unknown_remote_result' if last_error in {'network','timeout'} else
                'failed' if last_error else 'completed',
                {'response_id':raw_id,'usage':usage,'error':last_error})
            results.append({'id':raw_id,**raw})
        return results
