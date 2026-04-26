from __future__ import annotations

from .models import ACTIVE_STATUSES, AGENTS, ALL_STATUSES

ALLOWED_ROUTES = {
    ("jordan", "hermes"),
    ("hermes", "jordan"),
    ("jarvy", "hermes"),
    ("hermes", "jarvy"),
}
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


def require_agent(agent: str) -> None:
    if agent not in AGENTS:
        raise AccessPolicyError(f"unknown agent: {agent}")


def require_route(sender: str, recipient: str) -> None:
    require_agent(sender)
    require_agent(recipient)
    if (sender, recipient) not in ALLOWED_ROUTES:
        raise RoutePolicyError(f"route not allowed by default: {sender} -> {recipient}")


def require_actor_access(actor: str, sender: str, recipient: str) -> None:
    require_agent(actor)
    if actor not in {sender, recipient}:
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
    require_actor_access(actor, sender, recipient)
    return {
        f"incoming/{recipient}/{handoff_id}.md",
        f"outgoing/{sender}/{handoff_id}.md",
        f"archive/{handoff_id}/{handoff_id}.md",
    }
