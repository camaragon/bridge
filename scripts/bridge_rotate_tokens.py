#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from bridge_core.runtime import TOKEN_ENV_PREFIX, normalize_agent_env_suffix

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = Path(os.environ.get('BRIDGE_API_CONFIG', str(DEFAULT_ROOT / 'config' / 'bridge_api.env')))
DEFAULT_SERVICE = 'bridge-api.service'


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')


def read_env_file(path: Path) -> list[str]:
    if not path.exists():
        raise SystemExit(f'config not found: {path}')
    return path.read_text(encoding='utf-8').splitlines()


def token_key(agent: str) -> str:
    normalized = normalize_agent_env_suffix(agent)
    return f"{TOKEN_ENV_PREFIX}{normalized.replace('-', '_').upper()}"


def discover_agents(lines: list[str]) -> tuple[str, ...]:
    agents: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith(TOKEN_ENV_PREFIX) or '=' not in stripped:
            continue
        key = stripped.split('=', 1)[0]
        agent = normalize_agent_env_suffix(key[len(TOKEN_ENV_PREFIX):])
        if agent not in agents:
            agents.append(agent)
    return tuple(agents)


def rotate_lines(lines: list[str], agents: set[str]) -> list[str]:
    rotated = set()
    output: list[str] = []
    normalized_agents = {normalize_agent_env_suffix(agent) for agent in agents}
    for line in lines:
        replaced = False
        for agent in normalized_agents:
            key = token_key(agent)
            if line.startswith(f'{key}='):
                output.append(f'{key}={secrets.token_urlsafe(32)}')
                rotated.add(agent)
                replaced = True
                break
        if not replaced:
            output.append(line)
    missing = [agent for agent in sorted(normalized_agents) if agent not in rotated]
    if missing:
        if output and output[-1] != '':
            output.append('')
        for agent in missing:
            output.append(f'{token_key(agent)}={secrets.token_urlsafe(32)}')
    if output and output[-1] != '':
        output.append('')
    return output


def backup_path(config: Path) -> Path:
    return config.with_name(f'{config.name}.bak-{now_stamp()}')


def restart_service(unit: str) -> None:
    subprocess.run(['systemctl', '--user', 'restart', unit], check=True, timeout=30)
    subprocess.run(['systemctl', '--user', 'is-active', unit], check=True, timeout=30, capture_output=True, text=True)


def main() -> None:
    parser = argparse.ArgumentParser(description='Rotate Bridge API tokens safely without echoing secret values')
    parser.add_argument('--config', default=str(DEFAULT_CONFIG))
    parser.add_argument('--service', default=DEFAULT_SERVICE)
    parser.add_argument('--agents', nargs='+', default=['all'], help='agent ids to rotate, or all')
    parser.add_argument('--no-restart', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    config = Path(args.config)
    lines = read_env_file(config)
    available_agents = discover_agents(lines)
    selected = set(available_agents if 'all' in args.agents else [normalize_agent_env_suffix(agent) for agent in args.agents])
    new_lines = rotate_lines(lines, selected)
    backup = backup_path(config)

    if args.dry_run:
        print('dry_run: yes')
        print(f'config: {config}')
        print(f'backup_would_be: {backup}')
        print(f'agents: {", ".join(sorted(selected))}')
        print(f'restart_service: {"no" if args.no_restart else args.service}')
        return

    shutil.copy2(config, backup)
    backup.chmod(0o600)
    config.write_text('\n'.join(new_lines), encoding='utf-8')
    config.chmod(0o600)

    restarted = False
    if not args.no_restart:
        restart_service(args.service)
        restarted = True

    print(f'rotated_agents: {", ".join(sorted(selected))}')
    print(f'config: {config}')
    print(f'backup: {backup}')
    print(f'service_restarted: {"yes" if restarted else "no"}')


if __name__ == '__main__':
    main()
