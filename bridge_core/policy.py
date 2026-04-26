from __future__ import annotations

from .models import ACTIVE_STATUSES, ALL_STATUSES
from .runtime import configured_routes, normalize_agent_id

STATUS_ALIASES = {"acked": "acknowledged"}
STATUS_TRANSITIONS = {
    "open": {"open", "acknowledged", "in_progress", "blocked", "closed"},
    "acknowledged": {"acknowledged", "in_progress", "blocked", "closed"},
    "in_progress": {"in_progress", "blocked", "closed", "acknowledged"},
    "blocked": {"blocked", "in_progress", "closed", "acknowledged"},
    "closed": {"closed", "archived"},
    "archived": {"archived"},
}


class BridgePolicyError(ValueError):
    pass


class RoutePolicyError(BridgePolicyError):
    pass


class AccessPolicyError(BridgePolicyError):
    pass


class StatusPolicyError(BridgePolicyError):
    pass


def normalize_status(status: str) -> str:
    normalized = STATUS_ALIASES.get(status, status)
    if normalized not in ALL_STATUSES:
        raise StatusPolicyError(f"invalid status: {status}")
    return normalized


def require_agent(agent: str) -> str:
    try:
        return normalize_agent_id(agent)
    except ValueError as exc:
        raise AccessPolicyError(str(exc)) from exc


def allowed_routes() -> set[tuple[str, str]]:
    return configured_routes()


def require_route(sender: str, recipient: str) -> None:
    normalized_sender = require_agent(sender)
    normalized_recipient = require_agent(recipient)
    if normalized_sender == normalized_recipient:
        raise RoutePolicyError("sender and recipient must differ")
    routes = allowed_routes()
    if routes and (normalized_sender, normalized_recipient) not in routes:
        raise RoutePolicyError(f"route not allowed by policy: {normalized_sender} -> {normalized_recipient}")


def require_actor_access(actor: str, sender: str, recipient: str) -> None:
    normalized_actor = require_agent(actor)
    normalized_sender = require_agent(sender)
    normalized_recipient = require_agent(recipient)
    if normalized_actor not in {normalized_sender, normalized_recipient}:
        raise AccessPolicyError(f"actor {actor} is not sender or recipient for this handoff")


def require_status_transition(current: str, new: str, *, has_resolution_summary: bool) -> str:
    current_normalized = normalize_status(current)
    new_normalized = normalize_status(new)
    allowed = STATUS_TRANSITIONS[current_normalized]
    if new_normalized not in allowed:
        raise StatusPolicyError(f"invalid status transition: {current_normalized} -> {new_normalized}")
    if new_normalized == "closed" and not has_resolution_summary:
        raise StatusPolicyError("closing a handoff requires resolution summary")
    return new_normalized


def is_active_status(status: str) -> bool:
    return normalize_status(status) in ACTIVE_STATUSES


def visible_queues_for_actor(*, actor: str, sender: str, recipient: str, handoff_id: str) -> set[str]:
    normalized_actor = require_agent(actor)
    normalized_sender = require_agent(sender)
    normalized_recipient = require_agent(recipient)
    if normalized_actor not in {normalized_sender, normalized_recipient}:
        raise AccessPolicyError(f"actor {actor} is not sender or recipient for this handoff")
    return {
        f"incoming/{normalized_recipient}/{handoff_id}.md",
        f"outgoing/{normalized_sender}/{handoff_id}.md",
        f"archive/{handoff_id}/{handoff_id}.md",
    }
