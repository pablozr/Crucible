"""Admission application package.

The public surface stays stable: ``from
crucible_core.application.admissions import AdmissionCoordinator``
keeps working, with routing decisions, replay support and candidate
mutations available as submodules.
"""

from . import candidates, decisions, replay
from .coordinator import CAPTURE_DEADLINE_SECONDS, AdmissionCoordinator

__all__ = [
    "AdmissionCoordinator",
    "CAPTURE_DEADLINE_SECONDS",
    "candidates",
    "decisions",
    "replay",
]
