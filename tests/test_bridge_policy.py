from __future__ import annotations

import pytest

from bridge_core.policy import (
    RoutePolicyError,
    StatusPolicyError,
    normalize_status,
    require_route,
    require_status_transition,
    visible_queues_for_actor,
)


def test_require_route_allows_hermes_to_jarvy() -> None:
    require_route("hermes", "jarvy")


def test_require_route_rejects_jarvy_to_jordan() -> None:
    with pytest.raises(RoutePolicyError, match="route not allowed"):
        require_route("jarvy", "jordan")


def test_normalize_status_supports_acked_alias() -> None:
    assert normalize_status("acked") == "acknowledged"


def test_require_status_transition_rejects_invalid_close_without_outcome() -> None:
    with pytest.raises(StatusPolicyError, match="requires resolution summary"):
        require_status_transition("acknowledged", "closed", has_resolution_summary=False)


def test_visible_queues_for_actor_limits_to_sender_recipient_and_archive() -> None:
    paths = visible_queues_for_actor(
        actor="jordan",
        sender="hermes",
        recipient="jordan",
        handoff_id="HND-1",
    )

    assert paths == {
        "incoming/jordan/HND-1.md",
        "outgoing/hermes/HND-1.md",
        "archive/HND-1/HND-1.md",
    }
