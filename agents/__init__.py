"""PaperForge agents package.

This package hosts the next-generation orchestration layer while preserving
the current scientist / mvp / writeup behaviors defined by the existing codebase.
"""

from .coordinator import PaperForgeCoordinator
from .scientist_workflow_agent import ScientistWorkflowAgent
from .mvp_workflow_agent import MvpWorkflowAgent
from .writeup_agent import WriteupAgent

__all__ = [
    "PaperForgeCoordinator",
    "ScientistWorkflowAgent",
    "MvpWorkflowAgent",
    "WriteupAgent",
]
