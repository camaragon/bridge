from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import pwd
import re
import shlex

AGENT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def agent_id(value: str) -> str:
    if not AGENT_ID_PATTERN.fullmatch(value):
        raise ValueError(f"invalid agent id: {value!r}")
    return value


@dataclass(frozen=True)
class Agent:
    account: str
    uid: int
    notify: tuple[str, ...] = ()


def load_agents(root: Path) -> dict[str, Agent]:
    agents = {}
    for number, line in enumerate((root / "agents.conf").read_text(encoding="utf-8").splitlines(), 1):
        fields = shlex.split(line, comments=True)
        if not fields:
            continue
        if len(fields) < 2:
            raise ValueError(f"agents.conf:{number}: expected agent account [command ...]")
        name, account, *command = fields
        agent_id(name)
        if name in agents:
            raise ValueError(f"agents.conf:{number}: duplicate agent {name}")
        try:
            uid = pwd.getpwnam(account).pw_uid
        except KeyError:
            raise ValueError(f"agents.conf:{number}: unknown Unix account {account!r}") from None
        agents[name] = Agent(account, uid, tuple(command))
    return agents


def require_identity(agents: dict[str, Agent], actor: str) -> None:
    if actor not in agents:
        raise ValueError(f"unknown agent: {actor}")
    if os.getuid() != agents[actor].uid:
        raise ValueError(f"--as {actor} requires Unix account {agents[actor].account}")
