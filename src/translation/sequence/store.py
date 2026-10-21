"""One SQLite file per branch. CAS commits include workset and outbox heads."""
import json
from dataclasses import asdict

from ..narrative.schemas import NarrativeError
from ..narrative.store import NarrativeStore
from ..storage import digest


def insert(store,kind,data):
    ident=kind+':'+digest(data)
    store.db.execute('INSERT OR IGNORE INTO objects VALUES(?,?,?)',
        (ident,kind,json.dumps(data,ensure_ascii=False,sort_keys=True)))
    return ident


class SequenceRepository:
    def __init__(self,path):
        import threading
        self.store=NarrativeStore(path)
        self.db=self.store.db
        self._archive_lock=threading.Lock()
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS sequence_head (singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            run_id TEXT NOT NULL, head TEXT, checkpoint TEXT, workset TEXT, epoch INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS sequence_chapters (source_id TEXT PRIMARY KEY, position INTEGER NOT NULL,
            state TEXT NOT NULL, payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS sequence_commits (id TEXT PRIMARY KEY, source_id TEXT UNIQUE NOT NULL,
            idempotency_key TEXT UNIQUE NOT NULL);
          CREATE TABLE IF NOT EXISTS sequence_outbox (id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending');
          CREATE TABLE IF NOT EXISTS sequence_invalidations (object_id TEXT PRIMARY KEY, reason TEXT NOT NULL);
        ''')
        self.db.commit()

    def close(self): self.store.close()

    @property
    def run_id(self):
        value=getattr(self,'_run_id_cache',None)
        if value is None:
            with self.store._reader() as conn:
                row=conn.execute('SELECT run_id FROM sequence_head WHERE singleton=1').fetchone()
            if not row: raise NarrativeError('invalid_record','sequence has not been created')
            value=row[0]; self._run_id_cache=value
        return value

    @property
    def manifest(self):
        if not hasattr(self,'_manifest'):
            self._manifest=self.store.get(self.run_id,'sequence-run')
        return self._manifest

    def heads(self):
        with self.store._reader() as conn:
            row=conn.execute('SELECT head,checkpoint,workset,epoch FROM sequence_head WHERE singleton=1').fetchone()
        return dict(zip(('head','checkpoint','workset','epoch'),row))

    def chapters(self):
        with self.store._reader() as conn:
            rows=conn.execute('SELECT * FROM sequence_chapters ORDER BY position').fetchall()
        return [{'source_id':r[0],'position':r[1],'state':r[2],**json.loads(r[3])}
                for r in rows]

    def revision_limit(self, source_id, default):
        chapter = next(c for c in self.chapters() if c['source_id'] == source_id)
        limit, previous = default, None
        for ident in chapter.get('revision_extension_ids', []):
            extension = self.store.get(ident, 'sequence-revision-extension')
            if (extension['run_id'] != self.run_id or extension['source_id'] != source_id or
                    extension['previous'] != previous or extension['from_limit'] != limit or
                    not limit < extension['to_limit'] <= 8):
                raise NarrativeError('invalid_record', 'invalid revision budget extension chain')
            limit, previous = extension['to_limit'], ident
        return limit

    def stage(self,source_id,state,**payload):
        with self.store.lock:
            row=self.db.execute('SELECT state,payload FROM sequence_chapters WHERE source_id=?',(source_id,)).fetchone()
            if not row: raise NarrativeError('invalid_record','unknown sequence target')
            if row[0]=='committed':
                raise NarrativeError('conflict','cannot mutate committed chapter stage')
            value={**json.loads(row[1]),**payload}
            with self.db:
                self.db.execute('UPDATE sequence_chapters SET state=?,payload=? WHERE source_id=?',
                    (state,json.dumps(value,ensure_ascii=False,sort_keys=True),source_id))

    def commits(self):
        cached = getattr(self, '_commits_cache', None)
        if cached is None:
            with self.store._reader() as conn:
                rows=conn.execute(
                    'SELECT c.id FROM sequence_commits c JOIN sequence_chapters s '
                    'ON c.source_id=s.source_id ORDER BY s.position').fetchall()
            cached = [(r[0], self.store.get(r[0], 'chapter-commit')) for r in rows]
            self._commits_cache = cached
        return cached

    def inherited_commits(self):
        return [(r['id'],r['commit']) for r in self.manifest['inputs'].get('inherited_commits',[])]

    def all_commits(self): return self.inherited_commits()+self.commits()

    def validate_commit(self,commit_id):
        cache=getattr(self,'_validated_commits',None)
        if cache is None:
            cache={}; self._validated_commits=cache
        cached=cache.get(commit_id)
        if cached is not None:
            return cached
        commit=self.store.get(commit_id,'chapter-commit')
        inherited=dict(self.inherited_commits())
        if commit_id in inherited:
            if commit!=inherited[commit_id]: raise NarrativeError('stale_summary','inherited commit changed')
            cache[commit_id]=commit
            return commit
        with self.store._reader() as conn:
            committed=conn.execute('SELECT 1 FROM sequence_commits WHERE id=?',(commit_id,)).fetchone()
            invalidated=conn.execute('SELECT 1 FROM sequence_invalidations WHERE object_id=?',(commit_id,)).fetchone()
        if commit['run_id']!=self.run_id or not committed:
            raise NarrativeError('visibility_denied','foreign branch commit')
        if invalidated:
            raise NarrativeError('stale_summary','commit requires re-review')
        cache[commit_id]=commit
        return commit

    def commit_chapter(self,prepared_id,*,expected_head,idempotency_key,fault=None):
        with self.store.lock:
            return self._commit_chapter_locked(prepared_id,expected_head=expected_head,
                idempotency_key=idempotency_key,fault=fault)

    def _commit_chapter_locked(self,prepared_id,*,expected_head,idempotency_key,fault=None):
        prepared=self.store.get(prepared_id,'chapter-prepared')
        if prepared['run_id']!=self.run_id:
            raise NarrativeError('visibility_denied','prepared commit belongs to another branch')
        admission=self.store.get(prepared['admission_id'],'sequence-admission')
        version=self.store.get(prepared['version_id'],'translation-version')
        if (admission['run_id']!=self.run_id or admission['version_id']!=prepared['version_id'] or
                admission['review']!=prepared['review'] or not admission['working_eligible'] or
                admission['text_hash']!=prepared['text_hash'] or version['text']!=prepared['text'] or
                digest(prepared['text'])!=prepared['text_hash'] or version['finish_reason']!='stop'):
            raise NarrativeError('invalid_record','prepared text differs from admitted final version')
        review=prepared['review']
        checks=self.store.get(review['checks_id'],'translation-checks')
        report=self.store.get(review['report_id'],'review-report')
        if checks['hard_failures'] or not report['review_complete'] or not report['semantic_coverage_reviewed']:
            raise NarrativeError('review_blocked','incomplete admitted review')
        pending=[c for c in self.chapters() if c['state']!='committed']
        if pending and pending[0]['source_id']!=prepared['source_id'] and not self.db.execute(
                'SELECT 1 FROM sequence_commits WHERE idempotency_key=?',(idempotency_key,)).fetchone():
            raise NarrativeError('conflict','commit must follow the frozen chapter order')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            old=self.db.execute('SELECT id FROM sequence_commits WHERE idempotency_key=?',(idempotency_key,)).fetchone()
            if old:
                if self.store.get(old[0])['prepared_id']!=prepared_id:
                    raise NarrativeError('conflict','idempotency key reused for different prepared data')
                self.db.rollback(); return old[0]
            if self.heads()['head']!=expected_head or prepared['parent']!=expected_head:
                raise NarrativeError('conflict','sequence head changed')
            for dep in prepared.get('prior_commits',[]): self.validate_commit(dep)
            if fault: fault('before_commit')
            data={**prepared,'prepared_id':prepared_id,'idempotency_key':idempotency_key}
            commit_id=insert(self.store,'chapter-commit',data)
            workset=insert(self.store,'sequence-workset',{'run_id':self.run_id,'parent':self.heads()['workset'],
                'commit_id':commit_id,'unresolved':prepared['unresolved'],'knowledge_authority':'candidate_unreviewed'})
            self.db.execute('INSERT INTO sequence_commits VALUES(?,?,?)',(commit_id,prepared['source_id'],idempotency_key))
            event={'run_id':self.run_id,'commit_id':commit_id,'source_id':prepared['source_id']}
            event_id='sequence-event:'+digest(event)
            self.db.execute('INSERT INTO sequence_outbox(id,kind,payload) VALUES(?,?,?)',
                (event_id,'materialize_increment',json.dumps(event,ensure_ascii=False)))
            self.db.execute('UPDATE sequence_head SET head=?,workset=? WHERE singleton=1',(commit_id,workset))
            self.db.execute("UPDATE sequence_chapters SET state='committed',payload=? WHERE source_id=?",
                (json.dumps({'commit_id':commit_id, 'revision_extension_ids':
                    next(c for c in self.chapters() if c['source_id']==prepared['source_id']).get('revision_extension_ids',[])},
                    ensure_ascii=False),prepared['source_id']))
            if fault: fault('inside_commit')
            self.db.commit()
            self._commits_cache = None
        except BaseException:
            self.db.rollback(); raise
        if fault: fault('after_commit')
        return commit_id

    def switch_checkpoint(self,checkpoint_id,expected_head,*,fault=None):
        with self.store.lock:
            checkpoint=self.store.get(checkpoint_id,'sequence-checkpoint')
            if checkpoint['head']!=expected_head or checkpoint['run_id']!=self.run_id:
                raise NarrativeError('conflict','checkpoint binding differs')
            with self.db:
                if self.heads()['head']!=expected_head:
                    raise NarrativeError('conflict','head changed before checkpoint switch')
                if fault: fault('before_checkpoint_switch')
                self.db.execute('UPDATE sequence_head SET checkpoint=?,epoch=epoch+1 WHERE singleton=1',(checkpoint_id,))

    def invalidate_from(self,source_id,reason):
        with self.store.lock:
            commits=self.commits(); found=False; stale=[]
            for ident,c in commits:
                found |= c['source_id']==source_id
                if found: stale.append(ident)
            if not found: raise NarrativeError('invalid_record','cannot revise uncommitted chapter')
            # Node/checkpoint dependency closures contain exact commit references.
            for kind in ('sequence-node','sequence-checkpoint','sequence-workset','sequence-parent-summary'):
                for ident,value in self.store.objects(kind):
                    if set(value.get('commit_ids',[]))&set(stale) or value.get('commit_id') in stale:
                        stale.append(ident)
            with self.db:
                self.db.executemany('INSERT OR REPLACE INTO sequence_invalidations VALUES(?,?)',[(x,reason) for x in stale])
            self._latest_nodes_cache = None
            self._latest_incremental_nodes_cache = None
            self._validated_commits = {}
            self._generated_documents = {}
            self._commits_cache = None
            return stale


def create_sequence(path,spec,frozen_inputs,*,branch_name,provider_identity,counter_identity):
    """Inputs contain only permission-checked history and target Japanese."""
    repo=SequenceRepository(path)
    inputs=dict(frozen_inputs)
    targets=inputs['targets']; history=inputs.get('history_pairs',[])
    if not targets: raise ValueError('sequence requires targets')
    ids=[d['chapter_id'] for d in targets]
    orders=[d['order'] for d in targets]
    reserved=set(inputs.get('reserved_source_ids',[]))
    if len(set(ids))!=len(ids) or orders!=sorted(set(orders)) or set(ids)&reserved:
        raise NarrativeError('visibility_denied','targets duplicate, reordered or reserved')
    if any(d['language']!='ja' for d in targets) or any(p['source']['order']>=orders[0] or p['source']['chapter_id'] in reserved for p in history):
        raise NarrativeError('visibility_denied','history must be strictly before the window')
    if len({p['source']['chapter_id'] for p in history})!=len(history):
        raise NarrativeError('conflict','duplicate historical source pair')
    historical_sources=inputs.get('history_sources',[])
    if len({d['chapter_id'] for d in historical_sources})!=len(historical_sources):
        raise NarrativeError('conflict','duplicate historical source body')
    if any(d['language']!='ja' or d['order']>=orders[0] or d['chapter_id'] in reserved
           for d in historical_sources):
        raise NarrativeError('visibility_denied','historical source bodies must strictly precede targets')
    for pair in history:
        if pair['source']['language']!='ja' or not pair.get('translation') or any(d['language']!='zh' for d in pair['translation']):
            raise NarrativeError('invalid_record','historical full-pair context requires Japanese and Chinese bodies')
    for node in inputs.get('history_nodes',[]):
        if set(node['source_ids'])&set(ids) or node['end_position']>=orders[0] or set(node['source_ids'])&reserved:
            raise NarrativeError('visibility_denied','history node includes target, future or reserved range')
        if not node.get('dependencies'):
            raise NarrativeError('invalid_record','historical summaries require evidence dependencies')
        covered=set(node['source_ids'])
        ja_ids={d['chapter_id'] for d in node['dependencies'] if d['language']=='ja'}
        source_orders={d['chapter_id']:d['order'] for d in node['dependencies'] if d['language']=='ja'}
        if ja_ids!=covered or len(covered)!=len(node['source_ids']):
            raise NarrativeError('invalid_record','historical summary needs exact source dependency coverage')
        for doc in node['dependencies']:
            canonical=doc['chapter_id'] if doc['language']=='ja' else doc.get('canonical_source_id')
            if canonical not in covered or canonical in reserved or source_orders.get(canonical,orders[0])>=orders[0]:
                raise NarrativeError('visibility_denied','historical summary dependency leaks restricted text')
    spec_payload=asdict(spec)
    # Optional default added after the first P7 smoke freeze; preserve its run
    # identity rather than reinterpret saved responses as another branch.
    if spec_payload.get('planning_utilization')==.80: spec_payload.pop('planning_utilization')
    if spec_payload.get('translation_tools') is False: spec_payload.pop('translation_tools')
    if spec_payload.get('reference_profile') == 'legacy': spec_payload.pop('reference_profile')
    if spec_payload.get('mapping_profile') == 'audit-v2': spec_payload.pop('mapping_profile')
    if spec_payload.get('output_profile') == 'plain-v1': spec_payload.pop('output_profile')
    if spec_payload.get('child_min_content_tokens') == 0:
        spec_payload.pop('child_min_content_tokens')
        spec_payload.pop('parent_summary_input_tokens_per_child',None)
    manifest={'schema':'sequence-run-v1','branch_name':branch_name,'spec':spec_payload,
        'inputs':inputs,'provider':provider_identity,'counter':counter_identity,
        'prompt_version':'sequence-v1','hidden_reference_source_ids':ids,
        'formal_acceptance':False,'frozen_input_hash':digest(inputs)}
    run_id='sequence-run:'+digest(manifest)
    old=repo.db.execute('SELECT run_id FROM sequence_head WHERE singleton=1').fetchone()
    if old:
        if old[0]!=run_id:
            repo.close(); raise NarrativeError('conflict','branch file is bound to another run')
        return repo
    with repo.db:
        insert(repo.store,'sequence-run',manifest)
        for inherited in inputs.get('inherited_commits',[]):
            if insert(repo.store,'chapter-commit',inherited['commit'])!=inherited['id']:
                raise NarrativeError('stale_summary','inherited commit hash is invalid')
        repo.db.execute('INSERT INTO sequence_head(singleton,run_id) VALUES(1,?)',(run_id,))
        repo.db.executemany('INSERT INTO sequence_chapters VALUES(?,?,?,?)',
            [(d['chapter_id'],d['order'],'planned','{}') for d in targets])
    return repo
