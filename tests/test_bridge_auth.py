from __future__ import annotations

from pathlib import Path

import pytest

from bridge_core.auth import AuthenticationError, load_agent_tokens, require_agent_token


def test_load_agent_tokens_prefers_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_path = tmp_path / "tokens.env"
    config_path.write_text("BRIDGE_TOKEN_HERMES=config-hermes\nBRIDGE_TOKEN_JARVY=config-jarvy\n", encoding="utf-8")
    monkeypatch.setenv("BRIDGE_TOKEN_HERMES", "env-hermes")

    tokens = load_agent_tokens(config_path)

    assert tokens["hermes"] == "env-hermes"
    assert tokens["jarvy"] == "config-jarvy"
    assert "jordan" not in tokens


def test_require_agent_token_uses_constant_time_compare(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRIDGE_TOKEN_HERMES", "secret-value")

    require_agent_token("hermes", "secret-value")

    with pytest.raises(AuthenticationError, match="invalid token"):
        require_agent_token("hermes", "wrong")

    with pytest.raises(AuthenticationError, match="no configured token"):
        require_agent_token("jordan", "anything")
