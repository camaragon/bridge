#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from bridge_core.file_repository import FileBridgeRepository
from bridge_core.frontmatter import FrontmatterError, parse_frontmatter as core_parse_frontmatter, render_frontmatter
from bridge_core.models import AGENTS, ALL_STATUSES, ACTIVE_STATUSES, CreateHandoffInput, HandoffRecord
from bridge_core.policy import RoutePolicyError, require_actor_access, require_route, visible_queues_for_actor
from bridge_core.repository import HandoffNotFoundError
from bridge_core.service import BridgeService

ROOT = Path('/home/caragon/agent-shared')
BRIDGE = ROOT / 'bridge'
AUDIT = BRIDGE / 'audit' / 'handoff-log.md'
ALLOWED = {
    ('jordan', 'hermes'),
    ('hermes', 'jordan'),
    ('jarvy', 'hermes'),
    ('hermes', 'jarvy'),
}


def repository() -> FileBridgeRepository:
    return FileBridgeRepository(bridge_root=BRIDGE)


def service() -> BridgeService:
    return BridgeService(repository())


def validate_route(sender, recipient):
    try:
        require_route(sender, recipient)
    except RoutePolicyError as exc:
        raise SystemExit(str(exc)) from exc


def parse_frontmatter(text):
    normalized = text.replace('\r\n', '\n')
    if not normalized.startswith('---\n'):
        raise SystemExit('missing frontmatter')
    try:
        return core_parse_frontmatter(normalized)
    except FrontmatterError as exc:
        raise SystemExit(str(exc)) from exc


def _outbox_path(handoff_id: str, sender: str) -> Path:
    return BRIDGE / 'outgoing' / sender / f'{handoff_id}.md'


def _inbox_path(handoff_id: str, recipient: str) -> Path:
    return BRIDGE / 'incoming' / recipient / f'{handoff_id}.md'


def _load_records(handoff_id: str):
    try:
        return repository().load_records(handoff_id)
    except HandoffNotFoundError as exc:
        raise SystemExit(str(exc)) from exc


def _primary_record(handoff_id: str) -> HandoffRecord:
    try:
        return service().get_handoff(handoff_id)
    except HandoffNotFoundError as exc:
        raise SystemExit(str(exc)) from exc


def create(args):
    validate_route(args.sender, args.recipient)
    request = CreateHandoffInput(
        sender=args.sender,
        recipient=args.recipient,
        issue_type=args.issue_type,
        subject=args.subject,
        requested_action=args.requested_action,
        minimal_context=args.minimal_context,
        handoff_kind=args.handoff_kind,
        priority=args.priority,
        risk_level=args.risk_level,
        due_at=args.due_at or 'none',
        approval_needed=bool(args.approval_needed),
        approval_context=args.approval_context or 'none',
        response_format=args.response_format,
        related_paths=args.related_path or [],
        constraints=args.constraints or '- none',
    )
    try:
        record = service().create_handoff(request)
    except (ValueError, TypeError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps({
        'handoff_id': record.handoff_id,
        'outbox': str(_outbox_path(record.handoff_id, record.sender)),
        'inbox': str(_inbox_path(record.handoff_id, record.recipient)),
    }, indent=2))


def status(args):
    records = _load_records(args.handoff_id)
    primary = records[0].record
    try:
        require_actor_access(args.actor, primary.sender, primary.recipient)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    visible_targets = visible_queues_for_actor(
        actor=args.actor,
        sender=primary.sender,
        recipient=primary.recipient,
        handoff_id=args.handoff_id,
    )
    visible_paths = []
    for stored in records:
        relative_path = stored.path.relative_to(BRIDGE).as_posix()
        if relative_path in visible_targets or relative_path.startswith(f'archive/{args.handoff_id}/'):
            visible_paths.append(str(stored.path))
    print(json.dumps({
        'handoff_id': args.handoff_id,
        'actor': args.actor,
        'paths': visible_paths,
        'statuses': sorted({stored.record.status for stored in records}),
    }, indent=2))


def list_open(args):
    try:
        items = service().list_open_handoffs(args.agent)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    payload = [
        {
            'path': str(_inbox_path(item.handoff_id, args.agent)),
            'status': item.status,
            'subject': item.subject,
            'sender': item.sender,
            'recipient': item.recipient,
        }
        for item in items
        if item.status in ACTIVE_STATUSES
    ]
    print(json.dumps(payload, indent=2))


def set_status(args):
    try:
        record = service().set_status(args.handoff_id, actor=args.actor, status=args.status, outcome=args.outcome)
    except (HandoffNotFoundError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps({'handoff_id': record.handoff_id, 'actor': args.actor, 'status': record.status}, indent=2))


def archive(args):
    try:
        archive_dir = service().archive_handoff(args.handoff_id, actor=args.actor)
    except (HandoffNotFoundError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps({'handoff_id': args.handoff_id, 'actor': args.actor, 'archive_dir': str(archive_dir)}, indent=2))


def main():
    p = argparse.ArgumentParser(description='Agent bridge helper')
    sub = p.add_subparsers(dest='cmd', required=True)

    c = sub.add_parser('create')
    c.add_argument('--sender', required=True, choices=AGENTS)
    c.add_argument('--recipient', required=True, choices=AGENTS)
    c.add_argument('--issue-type', required=True)
    c.add_argument('--handoff-kind', default='request', choices=['incident', 'request', 'question', 'result'])
    c.add_argument('--priority', default='medium', choices=['low', 'medium', 'high', 'urgent'])
    c.add_argument('--risk-level', default='low', choices=['low', 'medium', 'high'])
    c.add_argument('--due-at', default='')
    c.add_argument('--subject', required=True)
    c.add_argument('--requested-action', required=True)
    c.add_argument('--minimal-context', required=True)
    c.add_argument('--constraints', default='')
    c.add_argument('--response-format', default='concise status + action + blocker if any')
    c.add_argument('--related-path', action='append')
    c.add_argument('--approval-needed', action='store_true')
    c.add_argument('--approval-context', default='')
    c.set_defaults(func=create)

    s = sub.add_parser('status')
    s.add_argument('--actor', required=True, choices=AGENTS)
    s.add_argument('handoff_id')
    s.set_defaults(func=status)

    l = sub.add_parser('list-open')
    l.add_argument('--agent', required=True, choices=AGENTS)
    l.set_defaults(func=list_open)

    u = sub.add_parser('set-status')
    u.add_argument('--actor', required=True, choices=AGENTS)
    u.add_argument('handoff_id')
    u.add_argument('status', choices=sorted(ALL_STATUSES))
    u.add_argument('--outcome', default='')
    u.set_defaults(func=set_status)

    a = sub.add_parser('archive')
    a.add_argument('--actor', required=True, choices=AGENTS)
    a.add_argument('handoff_id')
    a.set_defaults(func=archive)

    args = p.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
