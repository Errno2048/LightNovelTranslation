"""Permission checks distinguish corpus reference Chinese from branch output."""
from ..narrative.service import NarrativeView, dependency
from ..narrative.schemas import NarrativePolicy, NarrativeError
from ..storage import digest


class FrozenArchive:
    def __init__(self,inputs):
        self.chapters={}; self.rows={}; self.canonical_ids={}
        self.input_identity=inputs.get('corpus_identity',inputs.get('origin','frozen-sequence'))
        self.fingerprint=digest(inputs)
        for doc in inputs.get('history_sources',[]): self.add(doc,doc['chapter_id'])
        for pair in inputs.get('history_pairs',[]):
            self.add(pair['source'],pair['source']['chapter_id'])
            for doc in pair['translation']: self.add(doc,pair['source']['chapter_id'])
        for doc in inputs['targets']: self.add(doc,doc['chapter_id'])
        # Summary dependencies carry exact historical documents, including those
        # not retained as near full pairs. This is provenance, not prompt text.
        for node in inputs.get('history_nodes',[]):
            for doc in node['dependencies']:
                self.add(doc,doc.get('canonical_source_id',doc['chapter_id'] if doc['language']=='ja' else None))
        self.sources={c['match_key']:c for c in self.chapters.values() if c['language']=='ja'}

    def add(self,doc,canonical):
        if not canonical:
            raise NarrativeError('invalid_record','historical Chinese needs a canonical source binding')
        if doc.get('text_hash')!=digest(doc['text']) or '\n'.join(doc['paragraph_texts'])!=doc['text'] or len(doc['paragraph_ids'])!=len(doc['paragraph_texts']):
            raise NarrativeError('stale_summary','frozen body differs from paragraph coverage')
        cid=doc['chapter_id']
        record={**{k:v for k,v in doc.items() if k!='canonical_source_id'},'kind':'narrative'}
        if cid in self.chapters and self.chapters[cid]!=record:
            raise NarrativeError('conflict','two frozen document revisions')
        if cid in self.canonical_ids and self.canonical_ids[cid]!=canonical:
            raise NarrativeError('conflict','two canonical bindings for one frozen document')
        self.chapters[cid]=record; self.canonical_ids[cid]=canonical
        self.rows[cid]=[{'kind':'body','paragraph_id':pid,'text':text,'ordinal':i}
                       for i,(pid,text) in enumerate(zip(doc['paragraph_ids'],doc['paragraph_texts']))]

    def ensure_rows(self,ids):
        if any(cid not in self.rows for cid in ids):
            raise NarrativeError('visibility_denied','document absent from frozen allowed inputs')

    def canonical(self,chapter): return self.canonical_ids.get(chapter['chapter_id'])

    def check_allowed(self,chapter,policy):
        if self.canonical(chapter) in policy.reserved_source_ids:
            raise NarrativeError('visibility_denied','reserved source')


class SequenceNarrativeView(NarrativeView):
    def __init__(self,repo,target_id,counter):
        self.repo=repo
        manifest=repo.manifest; inputs=manifest['inputs']
        archive=FrozenArchive(inputs)
        if target_id is None:
            committed={c['source_id'] for _,c in repo.all_commits()}
            cutoff=max((archive.chapters[c]['order'] for c in committed),default=-1)
            self.maintenance_cutoff=cutoff
            target={'order':cutoff+1}
        else:
            target=archive.chapters[target_id]
        self.target_order=target['order']
        allowed=[p['source']['chapter_id'] for p in inputs.get('history_pairs',[])]
        for node in inputs.get('history_nodes',[]): allowed.extend(node['source_ids'])
        policy=NarrativePolicy(target_id=target_id,cutoff_order=target['order']-1 if target_id else None,
            reserved_source_ids=inputs.get('reserved_source_ids',[]),
            hidden_translation_source_ids=manifest['hidden_reference_source_ids'],allowed_translation_source_ids=allowed)
        super().__init__(archive,repo.store,policy,counter=counter)
        self.history={p['source']['chapter_id']:p for p in inputs.get('history_pairs',[])}
        self.index_translations={}
        for node in inputs.get('history_nodes',[]):
            for doc in node['dependencies']:
                if doc['language']=='zh':
                    cid=archive.canonical(archive.chapters[doc['chapter_id']])
                    ids=self.index_translations.setdefault(cid,[])
                    if doc['chapter_id'] not in ids: ids.append(doc['chapter_id'])
        for ident,c in repo.all_commits():
            if archive.chapters[c['source_id']]['order']<target['order']:
                doc=self.generated_document(ident)
                archive.add(doc,c['source_id'])

    def generated_document(self,commit_id):
        c=self.repo.validate_commit(commit_id)
        source=self.archive.chapters[c['source_id']]
        if source['order']>=self.target_order:
            raise NarrativeError('visibility_denied','generated text must precede the current task')
        if self.repo.manifest['spec']['mode']=='accepted_only' and not c.get('acceptance_id'):
            raise NarrativeError('visibility_denied','working text forbidden by accepted_only')
        if c.get('acceptance_id') and c['run_id']==self.repo.run_id:
            row=self.repo.db.execute('SELECT acceptance_id FROM accepted_heads WHERE source_id=?',(c['source_id'],)).fetchone()
            if not row or row[0]!=c['acceptance_id']:
                raise NarrativeError('stale_summary','formal acceptance has been superseded or revoked')
        text=c['text']; paragraphs=text.splitlines()
        return {'chapter_id':'generated:sequence:'+digest({'run':self.repo.run_id,'commit':commit_id})[:24],
            'revision_id':c['version_id'],'language':'zh','match_key':source['match_key'],'order':source['order'],
            'title':source['title'],'text':text,'text_hash':digest(text),
            'paragraph_ids':['generated:sequence:'+digest([commit_id,i])[:24] for i in range(len(paragraphs))],
            'paragraph_texts':paragraphs,'source':'generated_accepted' if c.get('acceptance_id') else 'generated_working',
            'canonical_source_id':c['source_id'],'commit_id':commit_id,'branch_id':self.repo.run_id,
            'quality_labels':c['quality_labels'],'unresolved':c['unresolved']}

    def body(self,chapter_id,*,task=False):
        if chapter_id.startswith('generated:sequence:'):
            for ident,c in self.repo.all_commits():
                expected='generated:sequence:'+digest({'run':self.repo.run_id,'commit':ident})[:24]
                if expected==chapter_id: return self.generated_document(ident)
            raise NarrativeError('visibility_denied','generated document is not from this branch')
        if chapter_id not in self.archive.chapters:
            raise NarrativeError('visibility_denied','reference not present in frozen allowed inputs')
        return super().body(chapter_id,task=task)

    def check(self,chapter,*,task=False,translation=False):
        if hasattr(self,'maintenance_cutoff'):
            canonical=self.archive.canonical(chapter)
            if not canonical or self.archive.chapters[canonical]['order']>self.maintenance_cutoff:
                raise NarrativeError('visibility_denied','maintenance view stops at the committed prefix')
        return super().check(chapter,task=task,translation=translation)

    def visible_sources(self):
        sources=super().visible_sources()
        if hasattr(self,'maintenance_cutoff'):
            sources=[c for c in sources if c['order']<=self.maintenance_cutoff]
        return sources

    def validate_generated_dependency(self,dep):
        doc=self.generated_document(dep['commit_id'])
        if dependency(doc)!=dep:
            raise NarrativeError('stale_summary','generated dependency differs from exact branch commit')
        return doc

    def validate_dependency(self,dep,*,task=False):
        if dep.get('commit_id'): return self.validate_generated_dependency(dep)
        return super().validate_dependency(dep,task=task)

    def read_index_candidate(self,node_id):
        from .hierarchy import enabled, plan_hierarchy, latest_nodes
        if enabled(self.repo):
            from .schemas import SequenceSpec
            spec=SequenceSpec.from_dict(self.repo.manifest['spec'])
            plan=plan_hierarchy(self.repo,spec,self)
            ready=latest_nodes(self.repo,allowed_source_ids=plan['ordered_source_ids'])
            node=next((n for n in ready if node_id in {n['id'],n['node_id']}),None)
            planned=next((n for n in plan['nodes'] if n['node_id']==node_id),None)
            generated=False
            if not node and planned and planned['level']==2 and hasattr(self,'detail_builder'):
                self.detail_builder([node_id]); generated=True
                node=next((n for n in latest_nodes(self.repo,allowed_source_ids=plan['ordered_source_ids'])
                           if n['node_id']==node_id),None)
            if node:
                for dep in node['dependencies']: self.validate_dependency(dep)
                return {'status':'found','node_id':node['node_id'],'revision_id':node['id'],
                    'level':node['level'],'build_method':node['build_method'],
                    'summary':node['summary'],'records':node['records'],
                    'semantic_status':node['semantic_status'],'extraction_status':node['state'],
                    'quarantined':node['quarantined'],'materialized_on_request':generated,
                    '_dependencies':node['dependencies']}
        candidates=[(i,n) for i,n in self.repo.store.objects('sequence-node') if node_id in {i,n['node_id']}]
        if not any(i==node_id for i,n in candidates):
            candidates=[(i,n) for i,n in candidates if n['end_position']<self.target_order]
        candidates.sort(key=lambda r:(r[1]['end_position'],len(r[1]['source_ids'])),reverse=True)
        for ident,node in candidates:
            if node_id in {ident,node['node_id']}:
                from .knowledge import node_is_valid
                node_is_valid(self.repo,node)
                if node['end_position']>=self.target_order:
                    raise NarrativeError('visibility_denied','future node')
                return {'status':'found','node_id':node['node_id'],'revision_id':ident,
                    'summary':node['summary'],'records':node['records'],'semantic_status':'candidate_unreviewed',
                    'extraction_status':node['state'],'quarantined':node['quarantined'],
                    '_dependencies':node['dependencies']}
        for node in self.repo.manifest['inputs'].get('history_nodes',[]):
            if node_id==node['id']:
                return {'status':'found','node_id':node_id,'summary':node['summary'],
                    'semantic_status':'candidate_unreviewed','_dependencies':[dependency(self.body(d['chapter_id'])) for d in node['dependencies']]}
        return {'status':'not_built','node_id':node_id,'generation_calls':0}

    def read_chapters(self,ids):
        commits={c['source_id']:ident for ident,c in self.repo.all_commits()}
        result=[]
        for cid in ids:
            source=self.body(cid)
            if cid in commits:
                doc=self.generated_document(commits[cid])
                result.append({'source':source,'translation':[doc],
                    'translation_status':doc['source'],'review_ids':[commits[cid]]})
            elif cid in self.history:
                pair=self.history[cid]
                docs=[self.body(d['chapter_id']) for d in pair['translation']]
                result.append({'source':source,'translation':docs,
                    'translation_status':'complete_range_coverage_semantics_unreviewed','review_ids':pair.get('review_ids',[])})
            elif cid in self.index_translations:
                # Frozen index dependencies are admitted chapter-bound bodies,
                # not paragraph alignments. body() applies every visibility gate.
                docs=[self.body(did) for did in self.index_translations[cid]]
                result.append({'source':source,'translation':docs,
                    'translation_status':'frozen_index_dependency_chapter_only_semantics_unreviewed','review_ids':[]})
            else:
                result.append({'source':source,'translation':[],'translation_status':'missing','review_ids':[]})
        return result

    def summary_view(self): return self
