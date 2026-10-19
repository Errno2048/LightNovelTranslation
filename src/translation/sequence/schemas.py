"""P7 branch and session contracts; working text never implies acceptance."""
from dataclasses import dataclass, field

from ..budget import ModelCapacity, RoleBudget


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
    planning_utilization: float = .80
    child_min_content_tokens: int = 0
    parent_summary_input_tokens_per_child: int = 8192
    compression_trigger: float = .85
    compression_target: float = .65
    soft_input_tokens: int | None = None
    max_requests: int = 300
    max_input_tokens: int = 20000000
    max_output_tokens: int = 3000000
    max_attempts: int = 3
    max_revisions: int = 1
    review_profile: str = 'audit-v6'
    translation_tools: bool = False
    reference_profile: str = 'stable-v1'
    mapping_profile: str = 'coarse-v1'
    targeted_evidence: bool = True
    targeted_evidence_tokens: int = 12288
    output_profile: str = 'plain-v1'

    def __post_init__(self):
        if self.output_profile not in {'plain-v1', 'source-records-v1'}:
            raise ValueError('unknown translation output profile')
        if self.output_profile == 'source-records-v1' and self.reference_profile != 'stable-v1':
            raise ValueError('source-bound output requires stable-v1 source references')
        if type(self.targeted_evidence) is not bool or type(self.targeted_evidence_tokens) is not int or self.targeted_evidence_tokens<=0:
            raise ValueError('invalid targeted evidence settings')
        if self.reference_profile not in {'legacy', 'stable-v1'} or self.mapping_profile not in {'audit-v2', 'coarse-v1'}:
            raise ValueError('unknown reference or mapping protocol')
        if self.strategy not in {'R','U','H'} or self.mode not in {'working','accepted_only'}:
            raise ValueError('invalid sequence strategy or mode')
        if not 0 < self.compression_target < self.compression_trigger <= 1:
            raise ValueError('invalid compression thresholds')
        if not 0<self.planning_utilization<=1:
            raise ValueError('invalid planning utilization')
        for name in ('n_chapter','n_child','child_max_chapters','parent_max_chapters',
                     'max_requests','max_input_tokens','max_output_tokens','max_attempts'):
            if type(getattr(self,name)) is not int or getattr(self,name)<=0:
                raise ValueError('sequence counts and budgets must be positive integers')
        if self.child_max_chapters>self.parent_max_chapters or not 1<=self.max_attempts<=3 or not 0<=self.max_revisions<=2:
            raise ValueError('invalid block bounds or retry limits')
        if type(self.child_min_content_tokens) is not int or self.child_min_content_tokens < 0:
            raise ValueError('child_min_content_tokens must be a nonnegative integer')
        if type(self.parent_summary_input_tokens_per_child) is not int or self.parent_summary_input_tokens_per_child <= 0:
            raise ValueError('parent summary input allowance must be a positive integer')
        if self.soft_input_tokens is not None and self.soft_input_tokens<=0:
            raise ValueError('invalid input soft budget')
        if self.review_profile not in {'audit-v5','audit-v6'}:
            raise ValueError('sequence requires a versioned audit-v5/v6 review profile')
        for role in ('translator','reviewer','adjudicator','indexer','compressor'):
            if role not in self.roles or self.roles[role].requested_output_tokens>self.capacity.max_output_tokens:
                raise ValueError('missing role or role output exceeds capacity')
        if any(v.requested_output_tokens>self.capacity.max_output_tokens for v in self.roles.values()):
            raise ValueError('auxiliary role output exceeds capacity')

    @classmethod
    def from_dict(cls, value):
        value=dict(value)
        value.setdefault('review_profile','audit-v5')
        value.setdefault('reference_profile','legacy')
        value.setdefault('mapping_profile','audit-v2')
        # Frozen pre-feature runs must retain their exact prompts and cache keys.
        value.setdefault('targeted_evidence',False)
        value.setdefault('output_profile','plain-v1')
        value['capacity']=ModelCapacity(**value['capacity'])
        value['roles']={k:RoleBudget(**v) for k,v in value['roles'].items()}
        return cls(**value)

    @classmethod
    def from_settings(cls,settings):
        if settings.generation.capacity is None:
            raise ValueError('P7 requires explicit [capacity] configuration')
        values=dict(settings.sequence)
        if settings.index.get('layout','adaptive-v1')!='adaptive-v1':
            raise ValueError('P7 currently supports adaptive-v1 for new partitions')
        mapping={'parent_max_chapters','child_max_chapters','planning_utilization',
                 'child_min_content_tokens','parent_summary_input_tokens_per_child'}
        values.update({k:v for k,v in settings.index.items() if k in mapping})
        defaults=default_roles()
        defaults.update(settings.roles)
        # New TOML runs inherit auxiliary budgets from the relevant main role.
        # from_dict retains old exact role dictionaries and request identities.
        for kind,role in {'mapping':'reviewer','revision':'translator','repair':'reviewer'}.items():
            if kind not in settings.roles: defaults[kind]=defaults[role]
        return cls(settings.generation.capacity,roles=defaults,**values)
