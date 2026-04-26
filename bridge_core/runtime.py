from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
import os
import re

AGENT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
ROUTE_ENV_VAR = "BRIDGE_ALLOWED_ROUTES"
AGENTS_ENV_VAR = "BRIDGE_AGENTS"
TOKEN_ENV_PREFIX = "BRIDGE_TOKEN_"
NOTIFY_URL_ENV_PREFIX = "BRIDGE_NOTIFY_URL_"
NOTIFY_EVENT_ENV_PREFIX = "BRIDGE_NOTIFY_EVENT_COMMAND_"
CONFIG_ENV_VAR = "BRIDGE_API_CONFIG"


def normalize_agent_id(agent: str, *, field_name: str = "agent") -> str:
    normalized = str(agent or "").strip().lower()
    if not normalized:
        raise ValueError(f"missing {field_name}")
    if not AGENT_ID_PATTERN.fullmatch(normalized):
        raise ValueError(
            f"invalid {field_name}: {agent!r}; expected lowercase letters, digits, hyphen, or underscore"
        )
    return normalized


def normalize_agent_env_suffix(value: str) -> str:
    return normalize_agent_id(str(value or "").strip().lower().replace("_", "-"))


def env_key_for_agent(prefix: str, agent: str) -> str:
    normalized = normalize_agent_id(agent)
    suffix = normalized.replace("-", "_").upper()
    return f"{prefix}{suffix}"


def _split_csv_like(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[\s,]+", value) if item.strip()]


def parse_agent_list(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    seen: list[str] = []
    for item in _split_csv_like(value):
        agent = normalize_agent_id(item)
        if agent not in seen:
            seen.append(agent)
    return tuple(seen)


def parse_allowed_routes(value: str | None) -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    if not value:
        return routes
    for raw_route in _split_csv_like(value):
        if ":" not in raw_route:
            raise ValueError(f"invalid route entry: {raw_route!r}; expected sender:recipient")
        sender, recipient = raw_route.split(":", 1)
        routes.add((normalize_agent_id(sender, field_name="sender"), normalize_agent_id(recipient, field_name="recipient")))
    return routes


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def configured_values() -> dict[str, str]:
    values: dict[str, str] = {}
    config_path = os.environ.get(CONFIG_ENV_VAR, "").strip()
    if config_path:
        path = Path(config_path)
        if path.exists():
            values.update(_read_env_file(path))
    for key, value in os.environ.items():
        if key.startswith("BRIDGE_"):
            values[key] = value
    return values


def configured_routes() -> set[tuple[str, str]]:
    return parse_allowed_routes(configured_values().get(ROUTE_ENV_VAR, ""))


def _agents_from_keyed_values(source: Iterable[str], prefix: str) -> set[str]:
    agents: set[str] = set()
    for key in source:
        if not key.startswith(prefix):
            continue
        suffix = key[len(prefix) :].strip()
        if suffix:
            agents.add(normalize_agent_env_suffix(suffix))
    return agents


def discover_agents(*, bridge_root: Path | str | None = None, config_values: Mapping[str, str] | None = None) -> tuple[str, ...]:
    values = dict(configured_values())
    if config_values:
        values.update(config_values)

    agents: set[str] = set(parse_agent_list(values.get(AGENTS_ENV_VAR, "")))
    agents.update(_agents_from_keyed_values(values.keys(), TOKEN_ENV_PREFIX))
    agents.update(_agents_from_keyed_values(values.keys(), NOTIFY_URL_ENV_PREFIX))
    agents.update(_agents_from_keyed_values(values.keys(), NOTIFY_EVENT_ENV_PREFIX))
    for sender, recipient in parse_allowed_routes(values.get(ROUTE_ENV_VAR, "")):
        agents.add(sender)
        agents.add(recipient)

    if bridge_root is not None:
        root = Path(bridge_root)
        for kind in ("incoming", "outgoing"):
            queue_root = root / kind
            if not queue_root.exists():
                continue
            for child in queue_root.iterdir():
                if child.is_dir():
                    agents.add(normalize_agent_id(child.name))

    return tuple(sorted(agents))
