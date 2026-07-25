"""Validated, evidence-producing domain plugins for PaperForge v3."""

from .base import DomainPlugin
from .bio import BiologyPlugin, BioPlugin
from .contracts import (
    DataValidationError,
    EvidenceRecord,
    PluginResult,
    ValidationIssue,
    VisualizationSpec,
)
from .cv import ComputerVisionPlugin, CVPlugin
from .nlp import NaturalLanguageProcessingPlugin, NLPPlugin
from .physics_material import (
    MaterialSciencePlugin,
    PhysicsMaterialPlugin,
    PhysicsMaterialsPlugin,
)
from .registry import (
    DomainPluginRegistry,
    builtin_registry,
    create_builtin_registry,
    default_registry,
    get_plugin,
    normalize_plugin_name,
    plugin_registry,
    run_plugin,
)
from .rl import ReinforcementLearningPlugin, RLPlugin
from .robotics import RoboticsPlugin, RobotLearningPlugin

__all__ = [
    "BioPlugin",
    "BiologyPlugin",
    "CVPlugin",
    "ComputerVisionPlugin",
    "DataValidationError",
    "DomainPlugin",
    "DomainPluginRegistry",
    "EvidenceRecord",
    "MaterialSciencePlugin",
    "NLPPlugin",
    "NaturalLanguageProcessingPlugin",
    "PhysicsMaterialPlugin",
    "PhysicsMaterialsPlugin",
    "PluginResult",
    "RLPlugin",
    "ReinforcementLearningPlugin",
    "RobotLearningPlugin",
    "RoboticsPlugin",
    "ValidationIssue",
    "VisualizationSpec",
    "builtin_registry",
    "create_builtin_registry",
    "default_registry",
    "get_plugin",
    "normalize_plugin_name",
    "plugin_registry",
    "run_plugin",
]
