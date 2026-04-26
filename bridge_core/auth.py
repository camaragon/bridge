from __future__ import annotations

from pathlib import Path
import hmac
import os

from .runtime import normalize_agent_env_suffix, normalize_agent_id

ENV_PREFIX = "BRIDGE_TOKEN_"


class AuthenticationError(ValueError):
    pass


def _read_config_tokens(config_path: Path | str | None) -> dict[str, str]:
    if config_path is None:
        return {}
    path = Path(config_path)
    if not path.exists():
        return {}
    tokens: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key.startswith(ENV_PREFIX):
            agent = normalize_agent_env_suffix(key[len(ENV_PREFIX) :])
            if value:
                tokens[agent] = value
    return tokens


def load_agent_tokens(config_path: Path | str | None = None) -> dict[str, str]:
    tokens = _read_config_tokens(config_path)
    for env_key, env_value in os.environ.items():
        if not env_key.startswith(ENV_PREFIX) or not env_value:
            continue
        agent = normalize_agent_env_suffix(env_key[len(ENV_PREFIX) :])
        tokens[agent] = env_value
    return tokens


def resolve_agent_from_token(presented_token: str, config_path: Path | str | None = None) -> str:
    if not presented_token:
        raise AuthenticationError("missing bridge token")
    matches = [
        agent
        for agent, expected in load_agent_tokens(config_path).items()
        if expected and hmac.compare_digest(expected, presented_token)
    ]
    if not matches:
        raise AuthenticationError("invalid bridge token")
    if len(matches) > 1:
        raise AuthenticationError("bridge token matches multiple agents")
    return matches[0]


def require_agent_token(agent: str, presented_token: str, config_path: Path | str | None = None) -> None:
    normalized_agent = normalize_agent_id(agent)
    tokens = load_agent_tokens(config_path)
    expected = tokens.get(normalized_agent)
    if not expected:
        raise AuthenticationError(f"no configured token for agent: {agent}")
    if not hmac.compare_digest(expected, presented_token):
        raise AuthenticationError(f"invalid token for agent: {agent}")
