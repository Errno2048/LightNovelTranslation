"""P7 branch and session contracts; working text never implies acceptance."""
from dataclasses import dataclass, field

from .budget import ModelCapacity, RoleBudget


def default_roles():
    return {'translator':RoleBudget(16384),'reviewer':RoleBudget(16384),'adjudicator':RoleBudget(16384),
            'indexer':RoleBudget(32768),'compressor':RoleBudget(16384),
            'mapping':RoleBudget(16384),'revision':RoleBudget(16384),'repair':RoleBudget(16384)}


@dataclass(frozen=True)
class SequenceSpec:
    capacity: ModelCapacity
    roles: dict = field(default_factory=default_roles)
    strategy: str = 'H'
    mode: str = 'working'
    n_chapter: int = 3
    n_child: int = 2
    child_max_chapters: int = 10
    parent_max_chapters: int = 100
    index_layout: str = 'token'
    parent_min_chapters: int | None = None
    expected_l2_count: int = 10
    l2_prepartition_min_chapters: int = 3
    parent_build_mode: str = 'direct'
    entity_catalog_threshold: int = 50
    core_entity_limit: int = 50
    planning_utilization: float = .80
    child_min_content_tokens: int = 0
    parent_summary_input_tokens_per_child: int = 8192
    compression_trigger: float = .85
    compression_target: float = .65
    soft_input_tokens: int | None = None
    max_requests: int = 0
    max_input_tokens: int = 0
    max_output_tokens: int = 0
    max_attempts: int = 3
    max_revisions: int = 1
    review_profile: str = 'chapter-review'
    translation_tools: bool = False
    reference_profile: str = 'stable'
    mapping_profile: str = 'coarse'
    targeted_evidence: bool = True
    targeted_evidence_tokens: int = 12288
    workset_max_tokens: int = 128000
    workset_recent_chapters: int = 25
    thread_reconcile: bool = False
    window_l2_blocks: int = 3
    translation_concurrency: int = 3
    review_concurrency: int = 4
    provisional_lookahead: int = 1
    output_profile: str = 'plain'

    def __post_init__(self):
        if self.parent_min_chapters is None:
            object.__setattr__(self, 'parent_min_chapters', min(30, self.parent_max_chapters))
        if self.output_profile not in {'plain', 'source-records'}:
            raise ValueError('unknown translation output profile')
        if self.output_profile == 'source-records' and self.reference_profile != 'stable':
            raise ValueError('source-bound output requires stable source references')
        if type(self.targeted_evidence) is not bool or type(self.targeted_evidence_tokens) is not int or self.targeted_evidence_tokens<=0:
            raise ValueError('invalid targeted evidence settings')
        if any(type(value) is not int or value<=0 for value in
               (self.workset_max_tokens,self.workset_recent_chapters)):
            raise ValueError('invalid workset budget settings')
        if type(self.thread_reconcile) is not bool:
            raise ValueError('thread_reconcile must be boolean')
        if any(type(value) is not int or value<=0 for value in
               (self.window_l2_blocks,self.translation_concurrency,self.review_concurrency)):
            raise ValueError('invalid window translation settings')
        if type(self.provisional_lookahead) is not int or self.provisional_lookahead<0:
            raise ValueError('provisional_lookahead must be a non-negative integer')
        if self.reference_profile not in {'plain', 'stable'} or self.mapping_profile not in {'coarse'}:
            raise ValueError('unknown reference or mapping protocol')
        if self.strategy not in {'R','U','H'} or self.mode not in {'working','accepted_only'}:
            raise ValueError('invalid sequence strategy or mode')
        if not 0 < self.compression_target < self.compression_trigger <= 1:
            raise ValueError('invalid compression thresholds')
        if not 0<self.planning_utilization<=1:
            raise ValueError('invalid planning utilization')
        for name in ('n_chapter','n_child','child_max_chapters','parent_max_chapters',
                     'parent_min_chapters','expected_l2_count','l2_prepartition_min_chapters',
                     'entity_catalog_threshold','core_entity_limit','max_attempts'):
            if type(getattr(self,name)) is not int or getattr(self,name)<=0:
                raise ValueError('sequence counts and budgets must be positive integers')
        if self.entity_catalog_threshold > self.core_entity_limit:
            raise ValueError('entity_catalog_threshold cannot exceed the core entity limit')
        for name in ('max_requests','max_input_tokens','max_output_tokens'):
            if type(getattr(self,name)) is not int or getattr(self,name)<0:
                raise ValueError('cumulative sequence budgets must be nonnegative; 0 means unlimited')
        if (self.child_max_chapters>self.parent_max_chapters or
                (self.index_layout == 'token' and self.parent_min_chapters>self.parent_max_chapters) or
                not 1<=self.max_attempts<=3 or not 0<=self.max_revisions<=2):
            raise ValueError('invalid block bounds or retry limits')
        if self.index_layout not in {'token', 'plain'}:
            raise ValueError('unknown index layout')
        if self.parent_build_mode not in {'direct', 'child_summary_rollup', 'direct_then_rollup'}:
            raise ValueError('invalid L1 build mode')
        if type(self.child_min_content_tokens) is not int or self.child_min_content_tokens < 0:
            raise ValueError('child_min_content_tokens must be a nonnegative integer')
        if type(self.parent_summary_input_tokens_per_child) is not int or self.parent_summary_input_tokens_per_child <= 0:
            raise ValueError('parent summary input allowance must be a positive integer')
        if self.soft_input_tokens is not None and self.soft_input_tokens<=0:
            raise ValueError('invalid input soft budget')
        if self.review_profile not in {'chapter-review'}:
            raise ValueError('sequence requires the chapter-review profile')
        for role in ('translator','reviewer','adjudicator','indexer','compressor'):
            if role not in self.roles or self.roles[role].requested_output_tokens>self.capacity.max_output_tokens:
                raise ValueError('missing role or role output exceeds capacity')
        if any(v.requested_output_tokens>self.capacity.max_output_tokens for v in self.roles.values()):
            raise ValueError('auxiliary role output exceeds capacity')

    @classmethod
    def from_dict(cls, value):
        value=dict(value)
        value.setdefault('index_layout', 'plain')
        # Frozen pre-feature jobs retain the historical capacity-fallback policy.
        value.setdefault('parent_min_chapters', min(30, value.get('parent_max_chapters', 100)))
        value.setdefault('parent_build_mode', 'direct_then_rollup')
        value.setdefault('review_profile','chapter-review')
        value.setdefault('reference_profile','plain')
        value.setdefault('mapping_profile','coarse')
        # Frozen pre-feature runs must retain their exact prompts and cache keys.
        value.setdefault('targeted_evidence',False)
        value.setdefault('workset_max_tokens',128000)
        value.setdefault('workset_recent_chapters',25)
        value.setdefault('thread_reconcile',False)
        value.setdefault('window_l2_blocks',3)
        value.setdefault('translation_concurrency',3)
        value.setdefault('review_concurrency',4)
        value.setdefault('provisional_lookahead',1)
        value.setdefault('output_profile','plain')
        value['capacity']=ModelCapacity(**value['capacity'])
        value['roles']={k:RoleBudget(**v) for k,v in value['roles'].items()}
        return cls(**value)

    @classmethod
    def from_settings(cls,settings):
        if settings.generation.capacity is None:
            raise ValueError('P7 requires explicit [capacity] configuration')
        values=dict(settings.sequence)
        if settings.index.get('layout','adaptive')!='adaptive':
            raise ValueError('P7 currently supports adaptive for new partitions')
        mapping={'parent_max_chapters','child_max_chapters','parent_min_chapters',
                 'expected_l2_count','l2_prepartition_min_chapters','entity_catalog_threshold',
                 'core_entity_limit','parent_build_mode','planning_utilization',
                 'child_min_content_tokens','parent_summary_input_tokens_per_child'}
        values.update({k:v for k,v in settings.index.items() if k in mapping})
        defaults=default_roles()
        defaults.update(settings.roles)
        # New TOML runs inherit auxiliary budgets from the relevant main role.
        # from_dict retains old exact role dictionaries and request identities.
        for kind,role in {'mapping':'reviewer','revision':'translator','repair':'reviewer'}.items():
            if kind not in settings.roles: defaults[kind]=defaults[role]
        return cls(settings.generation.capacity,roles=defaults,**values)
