"""Pure admission decisions without I/O.

The coordinator remains the sole owner of transactions, baseline
capture, revalidation and persistence. Everything here operates on
already observed values so the observable matrix stays identical.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from crucible_core.core.errors import AdmissionError

AdmissionRoute = Literal["join", "overlap", "candidate", "steer_without_task"]

NoInputOutcome = Literal["released_overlap", "steer_rejection"]


@dataclass(frozen=True)
class RoutingObservation:
    """Minimal typed context for the preliminary admit() branch."""

    delivery: str | None
    has_joinable: bool
    has_blocking: bool


@dataclass(frozen=True)
class TaskOwnership:
    """Minimal typed context for the owned-running-task check."""

    session_id: str
    tree_id: str
    status: str


def decide_route(observation: RoutingObservation) -> AdmissionRoute:
    """Map delivery + observed tasks to the next admission path."""
    if observation.delivery == "steer":
        if not observation.has_joinable:
            return "steer_without_task"
        return "join"
    if observation.has_joinable:
        return "join"
    if observation.has_blocking:
        return "overlap"
    return "candidate"


def is_owned_running_task(
    owner: TaskOwnership | None,
    session_id: str,
    tree_id: str,
) -> bool:
    """Check the repeated running-owner invariant without I/O."""
    return (
        owner is not None
        and owner.session_id == session_id
        and owner.tree_id == tree_id
        and owner.status == "running"
    )


def is_session_tree_mismatch(
    found_tree_id: str | None,
    current_tree_id: str | None,
) -> bool:
    """Pure part of the session/worktree guard.

    ``found_tree_id`` is ``None`` when the session was never seen, in
    which case there is nothing to mismatch.
    """
    if found_tree_id is None:
        return False
    return current_tree_id is None or found_tree_id != current_tree_id


def check_execution_match(
    stored_execution_id: str | None,
    incoming_execution_id: str | None,
) -> None:
    """Raise when the confirmed execution identity diverges."""
    if (
        not stored_execution_id
        or not incoming_execution_id
        or stored_execution_id != incoming_execution_id
    ):
        raise AdmissionError("EXECUTION_ID_MISMATCH", 409)


def decide_no_input_outcome(decision_outcome: str) -> NoInputOutcome:
    """Map a stored no-input decision to its replay path."""
    if decision_outcome == "released_overlap":
        return "released_overlap"
    return "steer_rejection"
