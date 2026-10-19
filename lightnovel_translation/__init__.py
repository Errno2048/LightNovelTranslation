"""Stable public facade for evidence-bound Japanese-to-Chinese novel translation."""
from translation.budget import ModelCapacity, RoleBudget
from translation.editions.index import EditionIndexSpec
from translation.ingestion import InputSpec, SourceSpec, ReferenceSetPlan, plan_reference_set
from translation.schemas import GenerationRequest, GenerationResult

from .config import Limits, ModelConfig, TranslationConfig
from .errors import ConfigurationError, TranslationError
from .index import Index
from .job import TranslationJob
from .models import ChapterInfo, RunResult
from .project import Edition, TranslationProject
from .user_config import init_user_config, user_config_directory, user_config_path, user_config_status
from .skill import export_skill

__version__ = "0.4.0"
__all__ = ["TranslationProject", "TranslationConfig", "ModelConfig", "ModelCapacity", "RoleBudget",
           "Limits", "EditionIndexSpec", "InputSpec", "SourceSpec", "Edition", "Index", "TranslationJob",
           "ChapterInfo", "RunResult", "GenerationRequest", "GenerationResult", "ReferenceSetPlan", "plan_reference_set",
           "ConfigurationError", "TranslationError", "init_user_config", "user_config_directory",
           "user_config_path", "user_config_status", "export_skill", "__version__"]
