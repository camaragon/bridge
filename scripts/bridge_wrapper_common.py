#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from bridge_core.auth import load_agent_tokens
from bridge_core.runtime import env_key_for_agent

DEFAULT_ROOT = Path(os.environ.get('BRIDGE_PROJECT_ROOT', str(SCRIPT_ROOT)))
DEFAULT_API_URL = 'http://127.0.0.1:8427'
DEFAULT_CONFIG_PATH = DEFAULT_ROOT / 'config' / 'bridge_api.env'
CLI_PATH = DEFAULT_ROOT / 'scripts' / 'bridge_cli.py'


def _is_truthy(value: str | None) -> bool:
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'on'}


def api_url() -> str:
    return os.environ.get('BRIDGE_WRAPPER_API_URL') or os.environ.get('BRIDGE_API_URL') or DEFAULT_API_URL


def token_config_path() -> Path | None:
    raw = os.environ.get('BRIDGE_WRAPPER_TOKEN_FILE') or os.environ.get('BRIDGE_API_CONFIG')
    if raw:
        return Path(raw)
    return DEFAULT_CONFIG_PATH


def bridge_root() -> Path:
    return Path(os.environ.get('BRIDGE_ROOT', str(DEFAULT_ROOT / 'bridge')))


def allow_cli_fallback(args: Namespace) -> bool:
    return bool(getattr(args, 'allow_cli_fallback', False) or _is_truthy(os.environ.get('BRIDGE_WRAPPER_ALLOW_CLI_FALLBACK')))


def load_token(agent: str) -> str:
    env_key = env_key_for_agent('BRIDGE_TOKEN_', agent)
    if os.environ.get(env_key):
        return os.environ[env_key]
    tokens = load_agent_tokens(token_config_path())
    token = tokens.get(agent)
    if token:
        return token
    raise SystemExit(f'missing bridge token for {agent}; set {env_key} or configure {token_config_path()}')


def api_request(agent: str, method: str, path: str, *, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None
    headers = {
        'Accept': 'application/json',
        'Authorization': f'Bearer {load_token(agent)}',
    }
    if payload is not None:
        data = json.dumps(payload).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    request = Request(f"{api_url().rstrip('/')}{path}", data=data, method=method, headers=headers)
    try:
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode('utf-8'))
    except HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace')
        try:
            payload = json.loads(detail)
            raise SystemExit(payload.get('detail', detail) or detail)
        except json.JSONDecodeError:
            raise SystemExit(detail or str(exc)) from exc
    except (URLError, OSError, TimeoutError) as exc:
        raise ConnectionError(str(exc)) from exc


def cli_fallback(agent: str, cli_args: list[str]) -> None:
    proc = subprocess.run(['python3', str(CLI_PATH)] + cli_args)
    raise SystemExit(proc.returncode)


def invoke(agent: str, args: Namespace, *, api_call, cli_args: list[str]) -> None:
    try:
        payload = api_call()
    except ConnectionError as exc:
        if not allow_cli_fallback(args):
            raise SystemExit(
                f'bridge API unavailable at {api_url()}; set --allow-cli-fallback or BRIDGE_WRAPPER_ALLOW_CLI_FALLBACK=1 to use direct CLI fallback ({exc})'
            ) from exc
        cli_fallback(agent, cli_args)
        return
    print(json.dumps(payload, indent=2))


def create_result(payload: dict[str, Any]) -> dict[str, Any]:
    handoff_id = str(payload['handoff_id'])
    sender = str(payload['sender'])
    recipient = str(payload['recipient'])
    root = bridge_root()
    return {
        'handoff_id': handoff_id,
        'outbox': str(root / 'outgoing' / sender / f'{handoff_id}.md'),
        'inbox': str(root / 'incoming' / recipient / f'{handoff_id}.md'),
    }


def list_result(agent: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    root = bridge_root()
    items = []
    for item in payload.get('items', []):
        handoff_id = str(item['handoff_id'])
        items.append({
            'path': str(root / 'incoming' / agent / f'{handoff_id}.md'),
            'status': item.get('status', ''),
            'subject': item.get('subject', ''),
            'sender': item.get('sender', ''),
            'recipient': item.get('recipient', ''),
        })
    return items


def status_result(agent: str, handoff_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    root = bridge_root()
    sender = str(payload.get('sender', ''))
    recipient = str(payload.get('recipient', ''))
    paths = []
    if agent == sender:
        paths.append(str(root / 'outgoing' / sender / f'{handoff_id}.md'))
    if agent == recipient:
        paths.append(str(root / 'incoming' / recipient / f'{handoff_id}.md'))
    archive_path = root / 'archive' / handoff_id / f'{handoff_id}.md'
    if archive_path.exists():
        paths.append(str(archive_path))
    return {
        'handoff_id': handoff_id,
        'actor': agent,
        'paths': paths,
        'statuses': sorted({str(payload.get('status', ''))}),
    }


def set_status_result(agent: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        'handoff_id': str(payload['handoff_id']),
        'actor': agent,
        'status': str(payload['status']),
    }


def archive_result(agent: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        'handoff_id': str(payload['handoff_id']),
        'actor': agent,
        'archive_dir': str(payload.get('archive_path') or (bridge_root() / 'archive' / payload['handoff_id'])),
    }
