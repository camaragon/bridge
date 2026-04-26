#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import sys

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from bridge_core.models import ACTIVE_STATUSES, now_iso
from bridge_core.policy import allowed_routes
from bridge_core.runtime import discover_agents
from bridge_core.tooling import LoadedHandoff, load_archive_entry, summarize_handoffs

ROOT = Path(os.environ.get('BRIDGE_PROJECT_ROOT', str(SCRIPT_ROOT)))
BRIDGE = Path(os.environ.get('BRIDGE_ROOT', str(ROOT / 'bridge')))
OUTPUT = Path(os.environ.get('BRIDGE_AUDIT_OUTPUT', str(BRIDGE / 'audit' / 'Bridge Audit View.md')))
ARCHIVE_OUTPUT = Path(os.environ.get('BRIDGE_ARCHIVE_OUTPUT', str(BRIDGE / 'audit' / 'Bridge Archive Index.md')))
ACTIVE = set(ACTIVE_STATUSES)
ARCHIVE_LIMIT = 25


def parse_iso_datetime(value: str):
    try:
        return datetime.fromisoformat((value or '').replace('Z', '+00:00'))
    except Exception:
        return None


def age_hours(updated_at: str) -> str:
    try:
        dt = parse_iso_datetime(updated_at)
        if dt is None:
            raise ValueError('missing datetime')
        delta = datetime.now(timezone.utc) - dt
        return f'{delta.total_seconds()/3600.0:.1f}h'
    except Exception:
        return 'unknown'


def route_label(item: LoadedHandoff) -> str:
    return f'{item.record.sender or "?"} → {item.record.recipient or "?"}'


def resolution_summary_for(item: LoadedHandoff) -> str:
    summary = (item.record.resolution_summary or '').strip()
    if summary:
        return summary
    marker = '## Outcome\n'
    if marker in item.body:
        outcome = item.body.split(marker, 1)[1].strip()
        if outcome:
            first_line = outcome.splitlines()[0].strip()
            if first_line:
                return first_line.lstrip('- ').strip()
    return 'pending'


def archived_sort_key(item: LoadedHandoff):
    updated = parse_iso_datetime(item.record.updated_at)
    if updated is not None:
        return updated
    return datetime.min.replace(tzinfo=timezone.utc)


def active_sort_key(item: LoadedHandoff):
    updated = parse_iso_datetime(item.record.updated_at)
    if updated is not None:
        return updated
    return datetime.max.replace(tzinfo=timezone.utc)


def priority_risk_label(item: LoadedHandoff) -> str:
    parts = []
    if item.record.priority and item.record.priority.lower() != 'none':
        parts.append(item.record.priority)
    if item.record.risk_level and item.record.risk_level.lower() != 'none':
        parts.append(f'{item.record.risk_level}-risk')
    return ' / '.join(parts)


def bullet_core(item: LoadedHandoff, include_updated: bool = True) -> str:
    hid = item.record.handoff_id or item.path.stem
    details = [f'`{hid}`', f'**{item.record.status or "?"}**']
    pr = priority_risk_label(item)
    if pr:
        details.append(pr)
    details.extend([route_label(item), item.record.subject or '(no subject)'])
    if include_updated:
        updated = item.record.updated_at or '?'
        details.append(f'updated `{updated}` ({age_hours(updated) if updated != "?" else "unknown"})')
    return '- ' + ' — '.join(details)


def bullet_for(item: LoadedHandoff):
    return bullet_core(item, include_updated=True)


def archive_bullet_for(item: LoadedHandoff):
    hid = item.record.handoff_id or item.path.stem
    details = [f'`{hid}`', '**archived**']
    pr = priority_risk_label(item)
    if pr:
        details.append(pr)
    details.extend([
        route_label(item),
        item.record.subject or '(no subject)',
        f'archived `{item.record.updated_at or "?"}`',
        resolution_summary_for(item),
    ])
    if item.archive_file_count > 1:
        details.append(f'preserved copies: {item.archive_file_count}')
    return '- ' + ' — '.join(details)


def build_archive_index(archived):
    recent = archived[:ARCHIVE_LIMIT]
    by_route = {}
    for item in recent:
        by_route.setdefault(route_label(item), []).append(item)

    lines = [
        '# Bridge Archive Index',
        '',
        f'> Auto-generated from `{ROOT}` for human audit. Edit source scripts, not this note.',
        '',
        f'- Last refreshed: `{now_iso()}`',
        f'- Archived handoffs included: **{len(recent)}**',
        '',
        '## Recent Closures',
    ]
    if not recent:
        lines.append('- none')
    else:
        lines.extend(archive_bullet_for(item) for item in recent)

    lines += ['', '## By Route']
    if not by_route:
        lines.append('- none')
    else:
        for route in sorted(by_route):
            lines += ['', f'### {route}']
            lines.extend(archive_bullet_for(item) for item in by_route[route])
    return '\n'.join(lines) + '\n'


def main():
    agents = list(discover_agents(bridge_root=BRIDGE))
    incoming = {agent: summarize_handoffs((BRIDGE / 'incoming' / agent).glob('*.md')) for agent in agents}
    archived = []
    for directory in sorted((BRIDGE / 'archive').glob('HND-*'), reverse=True):
        item = load_archive_entry(directory)
        if item:
            archived.append(item)
    archived.sort(key=archived_sort_key, reverse=True)
    archived = archived[:ARCHIVE_LIMIT]

    now = datetime.now(timezone.utc)
    active_items = [item for bucket in incoming.values() for item in bucket if item.record.status in ACTIVE]
    oldest_active = min(active_items, key=active_sort_key) if active_items else None
    blocked_items = [item for item in active_items if item.record.status == 'blocked']
    urgent_high_items = [item for item in active_items if (item.record.priority or '').strip().lower() in {'urgent', 'high'}]
    recent_closures = []
    for item in archived:
        updated_dt = parse_iso_datetime(item.record.updated_at)
        if updated_dt is not None and (now - updated_dt).total_seconds() <= 24 * 3600:
            recent_closures.append(item)

    route_violations = []
    stale = []
    routes = allowed_routes()
    for bucket in incoming.values():
        for item in bucket:
            if routes and (item.record.sender, item.record.recipient) not in routes:
                route_violations.append(item)
            if item.record.status in ACTIVE:
                dt = parse_iso_datetime(item.record.updated_at)
                if dt is not None:
                    hours = (now - dt).total_seconds() / 3600.0
                    if hours > 24:
                        stale.append((hours, item))
    stale.sort(reverse=True, key=lambda entry: entry[0])

    open_counts = {agent: sum(1 for item in bucket if item.record.status in ACTIVE) for agent, bucket in incoming.items()}
    total_open = sum(open_counts.values())
    needs_attention = []
    seen_attention = set()
    for item in blocked_items + urgent_high_items + [entry[1] for entry in stale] + route_violations:
        hid = item.record.handoff_id or item.path.stem
        if hid not in seen_attention:
            seen_attention.add(hid)
            needs_attention.append(item)
    oldest_summary = (
        f'`{oldest_active.record.handoff_id or oldest_active.path.stem}` ({age_hours(oldest_active.record.updated_at)})'
        if oldest_active else 'none'
    )
    agent_summary_lines = [f'- `{agent}` inbox active: **{open_counts.get(agent, 0)}**' for agent in agents] or ['- No agents discovered yet']

    lines = [
        '# Bridge Audit View',
        '',
        f'> Auto-generated from `{ROOT}` for human audit. Edit source scripts, not this note.',
        '',
        f'- Last refreshed: `{now_iso()}`',
        f'- Total active handoffs: **{total_open}**',
        *agent_summary_lines,
        f'- Oldest active handoff: {oldest_summary}',
        f'- Blocked handoffs: **{len(blocked_items)}**',
        f'- Urgent/high priority active: **{len(urgent_high_items)}**',
        f'- Closures in last 24h: **{len(recent_closures)}**',
        '',
        '## Current Health',
        f'- Route violations: **{len(route_violations)}**' if route_violations else '- Route violations: none',
        f'- Stale active handoffs (>24h): **{len(stale)}**' if stale else '- Stale active handoffs (>24h): none',
        '',
        '## Needs Attention',
    ]
    if not needs_attention:
        lines.append('- none')
    else:
        lines.extend(bullet_for(item) for item in needs_attention)

    lines += ['', '## Blocked Handoffs']
    if not blocked_items:
        lines.append('- none')
    else:
        lines.extend(bullet_for(item) for item in blocked_items)

    lines += ['', '## Oldest Active Handoff']
    lines.append('- none' if oldest_active is None else bullet_for(oldest_active))

    lines += ['', '## Recent Closures (24h)']
    if not recent_closures:
        lines.append('- none')
    else:
        lines.extend(archive_bullet_for(item) for item in recent_closures)

    for agent in agents:
        lines += ['', f'## {agent} Inbox']
        bucket = [item for item in incoming.get(agent, []) if item.record.status in ACTIVE]
        if not bucket:
            lines.append('- none')
        else:
            lines.extend(bullet_for(item) for item in bucket)

    lines += ['', '## Route Violations']
    if not route_violations:
        lines.append('- none')
    else:
        lines.extend(bullet_for(item) for item in route_violations)

    lines += ['', '## Stale Active Handoffs (>24h)']
    if not stale:
        lines.append('- none')
    else:
        lines.extend(bullet_for(item) for _hours, item in stale)

    lines += ['', '## Recent Archived Handoffs']
    if not archived:
        lines.append('- none')
    else:
        lines.extend(archive_bullet_for(item) for item in archived[:10])

    lines += [
        '',
        '## Quick Commands',
        '- `python3 ./scripts/bridge_agent.py --agent <agent-id> list-open`',
        '- `python3 ./scripts/bridge_agent.py --agent <agent-id> create --recipient <recipient-agent> ...`',
        '- `python3 ./scripts/bridge_patrol.py --stuck-hours 24`  # built-in 0.5h active unresolved alert + reminder/escalation checks',
    ]

    OUTPUT.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    OUTPUT.chmod(0o600)
    ARCHIVE_OUTPUT.write_text(build_archive_index(archived), encoding='utf-8')
    ARCHIVE_OUTPUT.chmod(0o600)
    print(f'wrote {OUTPUT}')
    print(f'wrote {ARCHIVE_OUTPUT}')


if __name__ == '__main__':
    main()
