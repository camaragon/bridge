#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from bridge_core.models import ACTIVE_STATUSES
from bridge_core.policy import allowed_routes
from bridge_core.runtime import discover_agents, env_key_for_agent
from bridge_core.tooling import summarize_handoffs

ROOT = Path(os.environ.get('BRIDGE_PROJECT_ROOT', str(SCRIPT_ROOT)))
BRIDGE = Path(os.environ.get('BRIDGE_ROOT', str(ROOT / 'bridge')))
CONFIG = Path(os.environ.get('BRIDGE_API_CONFIG', str(ROOT / 'config' / 'bridge_api.env')))
EXPECTED_DIR_MODE = '700'
EXPECTED_FILE_MODE = '600'
EXPECTED_SCRIPT_MODE = '700'
DEFAULT_API_URL = 'http://127.0.0.1:8427/v1/health'
DEFAULT_SYSTEMD_UNIT = 'bridge-api.service'
DEFAULT_REMINDER_AFTER_HOURS = 0.5
DEFAULT_REMINDER_REPEAT_HOURS = 2.0
DEFAULT_ESCALATE_AFTER_HOURS = 6.0
DEFAULT_ESCALATE_REPEAT_HOURS = 24.0
DEFAULT_ACTIVE_ALERT_HOURS = 0.5
DEFAULT_NOTIFY_TIMEOUT_SECONDS = 1.0
PATROL_STATE_PATH = Path(os.environ.get('BRIDGE_PATROL_STATE_PATH', str(BRIDGE / 'audit' / 'patrol-reminders.json')))
ACTIVE = set(ACTIVE_STATUSES)
FOLLOW_UP_STATUSES = {'open', 'acknowledged', 'in_progress', 'blocked'}
SUMMARY_PLACEHOLDERS = {'', 'pending', 'none', 'n/a', 'na'}


def unresolved_follow_up_reason(record) -> str | None:
    if record.status == 'open':
        if record.acknowledged_at == 'none':
            return 'unacknowledged_open_handoff'
        return None
    if record.status in FOLLOW_UP_STATUSES:
        return f'unresolved_{record.status}_handoff'
    return None


def resolution_summary_state(record) -> str:
    summary = (record.resolution_summary or '').strip()
    lowered = summary.lower()
    if lowered in SUMMARY_PLACEHOLDERS:
        return 'pending'
    if 'actively investigating' in lowered or lowered.startswith('investigating'):
        return 'investigating'
    return 'actionable'


def needs_active_alert(record) -> tuple[bool, str]:
    summary_state = resolution_summary_state(record)
    reason = unresolved_follow_up_reason(record)
    if reason is None:
        return False, summary_state
    return summary_state != 'actionable', summary_state


def iso_to_dt(value: str):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def mode_str(path: Path):
    return oct(path.stat().st_mode & 0o777)[2:]


def bridge_api_health_url() -> str:
    base = os.environ.get('BRIDGE_PATROL_API_URL')
    if base:
        return base
    if CONFIG.exists():
        values = _read_env_file(CONFIG)
        host = values.get('BRIDGE_API_HOST', '127.0.0.1')
        port = values.get('BRIDGE_API_PORT', '8427')
        return f'http://{host}:{port}/v1/health'
    return DEFAULT_API_URL


def bridge_systemd_unit() -> str:
    return os.environ.get('BRIDGE_PATROL_SYSTEMD_UNIT', DEFAULT_SYSTEMD_UNIT)


def patrol_state_path() -> Path:
    override = os.environ.get('BRIDGE_PATROL_STATE_PATH', '').strip()
    if override:
        return Path(override)
    return PATROL_STATE_PATH


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        values[key.strip()] = value.strip()
    return values


def _load_runtime_config() -> dict[str, str]:
    values: dict[str, str] = {}
    if CONFIG.exists():
        values.update(_read_env_file(CONFIG))
    for key, value in os.environ.items():
        if key.startswith('BRIDGE_'):
            values[key] = value
    return values


def _float_setting(key: str, default: float) -> float:
    runtime = _load_runtime_config()
    raw = str(runtime.get(key, default)).strip()
    try:
        return float(raw)
    except ValueError:
        return default


def load_patrol_state(path: Path) -> dict[str, dict[str, object]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    normalized: dict[str, dict[str, object]] = {}
    for handoff_id, item in payload.items():
        if isinstance(handoff_id, str) and isinstance(item, dict):
            normalized[handoff_id] = dict(item)
    return normalized


def save_patrol_state(path: Path, state: dict[str, dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    path.chmod(0o600)


def notify_recipient(url: str, *, token: str, payload: dict[str, object]) -> tuple[bool, str]:
    request = Request(
        url,
        method='POST',
        data=json.dumps(payload, sort_keys=True).encode('utf-8'),
        headers={
            'Accept': 'application/json',
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
        },
    )
    try:
        with urlopen(request, timeout=DEFAULT_NOTIFY_TIMEOUT_SECONDS) as response:
            status = int(getattr(response, 'status', 200))
            return 200 <= status < 300, f'notify status={status}'
    except HTTPError as exc:
        return False, f'notify failed status={exc.code}'
    except (URLError, TimeoutError, OSError) as exc:
        return False, f'notify failed ({exc})'


def maybe_trigger_ack_reminder(
    record,
    *,
    now: datetime,
    runtime_config: dict[str, str],
    state: dict[str, dict[str, object]],
    reminder_after_hours: float,
    reminder_repeat_hours: float,
) -> tuple[bool, str | None]:
    reason = unresolved_follow_up_reason(record)
    if reason is None or not record.updated_at:
        return False, None
    age_hours = (now - iso_to_dt(record.updated_at)).total_seconds() / 3600.0
    if age_hours < reminder_after_hours:
        return False, None
    reminder_state = state.setdefault(record.handoff_id, {})
    last_reminded_at = str(reminder_state.get('last_reminded_at', '') or '')
    if last_reminded_at:
        elapsed = (now - iso_to_dt(last_reminded_at)).total_seconds() / 3600.0
        if elapsed < reminder_repeat_hours:
            return False, None
    notify_url = runtime_config.get(env_key_for_agent('BRIDGE_NOTIFY_URL_', record.recipient), '').strip()
    token = runtime_config.get(env_key_for_agent('BRIDGE_TOKEN_', record.recipient), '').strip()
    if not notify_url:
        return False, f'reminder skipped for {record.handoff_id}: no notify endpoint configured for {record.recipient}'
    if not token:
        return False, f'reminder skipped for {record.handoff_id}: no token configured for {record.recipient}'
    reminded_at = now.replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    ok, detail = notify_recipient(
        notify_url,
        token=token,
        payload={
            'trigger': 'handoff_reminder',
            'reason': reason,
            'handoff_id': record.handoff_id,
            'recipient': record.recipient,
            'sender': record.sender,
            'subject': record.subject,
            'status': record.status,
            'age_hours': round(age_hours, 2),
            'reminder_after_hours': reminder_after_hours,
        },
    )
    if not ok:
        return False, f'reminder notify failed for {record.handoff_id}: {detail}'
    reminder_state['last_reminded_at'] = reminded_at
    reminder_state['reminder_count'] = int(reminder_state.get('reminder_count', 0) or 0) + 1
    reminder_state['last_reminder_detail'] = detail
    return True, (
        f'reminder sent for {record.handoff_id} to {record.recipient} '
        f'status={record.status} age={age_hours:.1f}h count={reminder_state["reminder_count"]}'
    )


def maybe_mark_escalation(
    record,
    *,
    now: datetime,
    state: dict[str, dict[str, object]],
    escalate_after_hours: float,
    escalate_repeat_hours: float,
) -> str | None:
    reason = unresolved_follow_up_reason(record)
    if reason is None or not record.updated_at:
        return None
    age_hours = (now - iso_to_dt(record.updated_at)).total_seconds() / 3600.0
    if age_hours < escalate_after_hours:
        return None
    reminder_state = state.setdefault(record.handoff_id, {})
    escalated_at = str(reminder_state.get('escalated_at', '') or '')
    if escalated_at:
        elapsed = (now - iso_to_dt(escalated_at)).total_seconds() / 3600.0
        if elapsed < escalate_repeat_hours:
            return None
    mark = now.replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    reminder_state['escalated_at'] = mark
    reminder_state['escalation_count'] = int(reminder_state.get('escalation_count', 0) or 0) + 1
    return (
        f'escalation: unresolved {record.status} handoff for {record.recipient}: {record.handoff_id} '
        f'age={age_hours:.1f}h subject={record.subject or "(no subject)"}'
    )


def check_systemd_service(unit: str) -> tuple[str, str]:
    try:
        proc = subprocess.run(
            ['systemctl', '--user', 'is-active', unit],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except FileNotFoundError:
        return 'warning', 'systemctl not available for bridge API check'
    except subprocess.TimeoutExpired:
        return 'warning', f'systemd check timed out for {unit}'
    state = (proc.stdout or proc.stderr or '').strip() or 'unknown'
    if proc.returncode == 0 and state == 'active':
        return 'ok', f'{unit} active'
    return 'warning', f'{unit} not active ({state})'


def check_api_health(url: str) -> tuple[str, str]:
    try:
        with urlopen(url, timeout=5) as response:
            payload = json.loads(response.read().decode('utf-8'))
        if payload.get('ok') is True:
            return 'ok', f'bridge API health ok at {url}'
        return 'warning', f'bridge API unhealthy payload at {url}'
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return 'warning', f'bridge API health failed at {url} ({exc})'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--stuck-hours', type=float, default=24.0)
    ap.add_argument('--active-alert-hours', type=float, default=_float_setting('BRIDGE_PATROL_ACTIVE_ALERT_HOURS', DEFAULT_ACTIVE_ALERT_HOURS))
    ap.add_argument('--reminder-after-hours', type=float, default=_float_setting('BRIDGE_PATROL_REMINDER_AFTER_HOURS', DEFAULT_REMINDER_AFTER_HOURS))
    ap.add_argument('--reminder-repeat-hours', type=float, default=_float_setting('BRIDGE_PATROL_REMINDER_REPEAT_HOURS', DEFAULT_REMINDER_REPEAT_HOURS))
    ap.add_argument('--escalate-after-hours', type=float, default=_float_setting('BRIDGE_PATROL_ESCALATE_AFTER_HOURS', DEFAULT_ESCALATE_AFTER_HOURS))
    ap.add_argument('--escalate-repeat-hours', type=float, default=_float_setting('BRIDGE_PATROL_ESCALATE_REPEAT_HOURS', DEFAULT_ESCALATE_REPEAT_HOURS))
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    warnings = []
    checks = []
    evidence = []
    runtime_config = _load_runtime_config()
    state_path = patrol_state_path()
    reminder_state = load_patrol_state(state_path)
    state_changed = False

    critical_dirs = [
        ROOT,
        ROOT / 'core',
        ROOT / 'bridge',
        ROOT / 'bridge' / 'incoming',
        ROOT / 'bridge' / 'outgoing',
        ROOT / 'bridge' / 'archive',
        ROOT / 'bridge' / 'audit',
        ROOT / 'scripts',
    ]
    agents = list(discover_agents(bridge_root=BRIDGE, config_values=runtime_config))
    for agent in agents:
        critical_dirs.append(ROOT / 'bridge' / 'incoming' / agent)
        critical_dirs.append(ROOT / 'bridge' / 'outgoing' / agent)
    for directory in critical_dirs:
        checks.append(f'checked dir {directory}')
        if not directory.exists():
            warnings.append(f'missing dir: {directory}')
        elif mode_str(directory) != EXPECTED_DIR_MODE:
            warnings.append(f'bad mode {mode_str(directory)} on {directory}')

    if not CONFIG.exists():
        warnings.append(f'missing config file: {CONFIG}')
    elif mode_str(CONFIG) != EXPECTED_FILE_MODE:
        warnings.append(f'bad mode {mode_str(CONFIG)} on {CONFIG}')
    else:
        checks.append(f'checked config {CONFIG}')

    md_files = [path for path in ROOT.glob('**/*.md') if '.pytest_cache' not in path.parts]
    for file_path in md_files:
        if mode_str(file_path) != EXPECTED_FILE_MODE:
            warnings.append(f'bad mode {mode_str(file_path)} on {file_path}')

    py_files = list((ROOT / 'scripts').glob('*.py'))
    for file_path in py_files:
        if mode_str(file_path) != EXPECTED_SCRIPT_MODE:
            warnings.append(f'bad mode {mode_str(file_path)} on {file_path}')

    unit = bridge_systemd_unit()
    service_state, service_message = check_systemd_service(unit)
    checks.append(f'checked systemd user unit {unit}')
    evidence.append(service_message)
    if service_state != 'ok':
        warnings.append(service_message)

    health_url = bridge_api_health_url()
    api_state, api_message = check_api_health(health_url)
    checks.append(f'checked API health {health_url}')
    evidence.append(api_message)
    if api_state != 'ok':
        warnings.append(api_message)

    summarized = list(summarize_handoffs(BRIDGE.glob('incoming/*/*.md')))
    open_counts = {agent: 0 for agent in agents}
    routes = allowed_routes()
    for item in summarized:
        record = item.record
        if routes and (record.sender, record.recipient) not in routes:
            warnings.append(f'disallowed route in {item.path}: {record.sender}->{record.recipient}')
        if record.recipient in open_counts and record.status in ACTIVE:
            open_counts[record.recipient] += 1
            if record.updated_at:
                age_hours = (now - iso_to_dt(record.updated_at)).total_seconds() / 3600.0
                should_alert, summary_state = needs_active_alert(record)
                if should_alert and age_hours > max(args.active_alert_hours, 0.0):
                    warnings.append(
                        f'active unresolved handoff for {record.recipient}: {record.handoff_id} '
                        f'age={age_hours:.1f}h status={record.status} summary={summary_state} '
                        f'subject={record.subject or "(no subject)"}'
                    )
                if age_hours > args.stuck_hours:
                    warnings.append(
                        f'stale {record.status} handoff for {record.recipient}: {item.path.name} age={age_hours:.1f}h subject={record.subject or "(no subject)"}'
                    )
            reminded, reminder_message = maybe_trigger_ack_reminder(
                record,
                now=now,
                runtime_config=runtime_config,
                state=reminder_state,
                reminder_after_hours=max(args.reminder_after_hours, 0.0),
                reminder_repeat_hours=max(args.reminder_repeat_hours, 0.0),
            )
            if reminder_message:
                evidence.append(reminder_message)
            if reminded:
                state_changed = True
            escalation_warning = maybe_mark_escalation(
                record,
                now=now,
                state=reminder_state,
                escalate_after_hours=max(args.escalate_after_hours, 0.0),
                escalate_repeat_hours=max(args.escalate_repeat_hours, 0.0),
            )
            if escalation_warning:
                warnings.append(escalation_warning)
                state_changed = True

    active_ids = {item.record.handoff_id for item in summarized if item.record.status in ACTIVE}
    stale_state_keys = [handoff_id for handoff_id in reminder_state if handoff_id not in active_ids]
    for handoff_id in stale_state_keys:
        reminder_state.pop(handoff_id, None)
        state_changed = True

    if state_changed:
        save_patrol_state(state_path, reminder_state)

    open_count_summary = ' '.join(f'{agent}={open_counts[agent]}' for agent in agents) if agents else 'none'
    evidence.append(f'open counts {open_count_summary}')
    evidence.append(f'markdown files checked={len(md_files)}')
    evidence.append(f'script files checked={len(py_files)}')
    evidence.append(
        'ack reminder policy '
        f"after={max(args.reminder_after_hours, 0.0):.2f}h "
        f"repeat={max(args.reminder_repeat_hours, 0.0):.2f}h "
        f"escalate={max(args.escalate_after_hours, 0.0):.2f}h "
        f"repeat={max(args.escalate_repeat_hours, 0.0):.2f}h"
    )
    evidence.append(f'active handoff alert threshold={max(args.active_alert_hours, 0.0):.2f}h')

    status = 'ok' if not warnings else 'warning'
    escalation = '- none' if not warnings else '- review warnings; tighten or triage as needed'
    next_step = '- none' if not warnings else '- inspect stale handoffs, service health, or permission drift'

    print(f'status: {status}')
    print('scope: bridge patrol')
    print('checks:')
    for item in checks[:12]:
        print(f'- {item}')
    if len(checks) > 12:
        print(f'- ... ({len(checks) - 12} more checks)')
    print('actions_taken:')
    print('- scanned bridge directories, markdown files, and incoming handoffs')
    print('- checked route policy, Bridge API health, service state, file permission posture, active handoff aging, and unresolved-handoff reminders')
    print('evidence:')
    for item in evidence:
        print(f'- {item}')
    for item in warnings[:8]:
        print(f'- {item}')
    if len(warnings) > 8:
        print(f'- ... ({len(warnings) - 8} more warnings)')
    print('escalation:')
    print(escalation)
    print('next_suggested_step:')
    print(next_step)


if __name__ == '__main__':
    main()
