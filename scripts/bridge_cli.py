#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.request import Request, urlopen

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from bridge_core.file_repository import FileBridgeRepository
from bridge_core.frontmatter import FrontmatterError, parse_frontmatter as core_parse_frontmatter
from bridge_core.models import ALL_STATUSES, ACTIVE_STATUSES, CreateHandoffInput, HandoffRecord
from bridge_core.policy import RoutePolicyError, require_actor_access, require_route, visible_queues_for_actor
from bridge_core.repository import HandoffNotFoundError
from bridge_core.auth import load_agent_tokens
from bridge_core.runtime import configured_values, env_key_for_agent
from bridge_core.service import BridgeService

ROOT = Path(os.environ.get('BRIDGE_PROJECT_ROOT', str(SCRIPT_ROOT)))
BRIDGE = Path(os.environ.get('BRIDGE_ROOT', str(ROOT / 'bridge')))
AUDIT = BRIDGE / 'audit' / 'handoff-log.md'


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
        idempotency_key=getattr(args, 'idempotency_key', None),
        correlation_id=getattr(args, 'correlation_id', None),
        causation_id=getattr(args, 'causation_id', None),
    )
    try:
        record = service().create_handoff(request)
    except (ValueError, TypeError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps({
        'handoff_id': record.handoff_id,
        'outbox': str(_outbox_path(record.handoff_id, record.sender)),
        'inbox': str(_inbox_path(record.handoff_id, record.recipient)),
        'revision': record.revision,
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
        'revisions': sorted({stored.record.revision for stored in records}),
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
        record = service().set_status(
            args.handoff_id,
            actor=args.actor,
            status=args.status,
            outcome=args.outcome,
            expected_revision=getattr(args, 'expected_revision', None),
        )
    except (HandoffNotFoundError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps({'handoff_id': record.handoff_id, 'actor': args.actor, 'status': record.status, 'revision': record.revision}, indent=2))


def archive(args):
    try:
        archive_dir = service().archive_handoff(
            args.handoff_id,
            actor=args.actor,
            expected_revision=getattr(args, 'expected_revision', None),
        )
    except (HandoffNotFoundError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps({'handoff_id': args.handoff_id, 'actor': args.actor, 'archive_dir': str(archive_dir)}, indent=2))


def outbox_list(args):
    states = set(args.state) if args.state else None
    print(json.dumps([event.to_dict() for event in service().list_outbox(states=states)], indent=2, sort_keys=True))


def outbox_replay(args: argparse.Namespace) -> None:
    event = service().replay_event(args.event_id)
    print(json.dumps(event.to_dict(), indent=2, sort_keys=True))


def outbox_resolve_transaction(args: argparse.Namespace) -> None:
    event = service().resolve_transaction(
        args.event_id,
        action=args.action,
        reason=args.reason,
        force_recoverable_discard=args.force_recoverable_discard,
    )
    print(
        json.dumps(
            {"event": event.to_dict(), "action": args.action, "reason": args.reason},
            indent=2,
            sort_keys=True,
        )
    )


def reconcile(args):
    repo = repository()
    if args.handoff_id:
        result = {args.handoff_id: repo.reconcile_handoff(args.handoff_id, repair=not args.check_only)}
    else:
        ids = {path.stem for path in repo.records_root.glob('*.md')}
        ids.update(path.stem for path in BRIDGE.glob('incoming/*/*.md'))
        ids.update(path.stem for path in BRIDGE.glob('outgoing/*/*.md'))
        result = {handoff_id: repo.reconcile_handoff(handoff_id, repair=not args.check_only) for handoff_id in sorted(ids)}
    print(json.dumps(result, indent=2, sort_keys=True))


def recover_outbox(_args):
    recovered = repository().recover_transactions()
    print(json.dumps({'recovered_event_ids': recovered}, indent=2, sort_keys=True))


def deliver_outbox(args):
    repo = repository()
    bridge_service = BridgeService(repo)
    config_path = Path(os.environ['BRIDGE_API_CONFIG']) if os.environ.get('BRIDGE_API_CONFIG') else ROOT / 'config' / 'bridge_api.env'
    os.environ.setdefault('BRIDGE_API_CONFIG', str(config_path))
    values = configured_values()
    tokens = load_agent_tokens(config_path)

    def deliver(event):
        url = values.get(env_key_for_agent('BRIDGE_NOTIFY_URL_', event.target_agent), '').strip()
        token = tokens.get(event.target_agent, '')
        if not url:
            return False, f'no notify endpoint configured for {event.target_agent}'
        if not token:
            return False, f'no token configured for {event.target_agent}'
        request = Request(
            url,
            method='POST',
            data=json.dumps(event.to_dict(), sort_keys=True).encode('utf-8'),
            headers={
                'Accept': 'application/json',
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json',
                'X-Bridge-Event-ID': event.event_id,
            },
        )
        try:
            with urlopen(request, timeout=args.timeout):
                return True, 'delivered'
        except Exception as exc:
            return False, str(exc)

    processed = bridge_service.process_outbox(
        deliver,
        limit=args.limit,
        max_attempts=args.max_attempts,
        base_delay_seconds=args.base_delay_seconds,
        max_delay_seconds=args.max_delay_seconds,
    )
    print(json.dumps([event.to_dict() for event in processed], indent=2, sort_keys=True))


def main():
    p = argparse.ArgumentParser(description='Agent bridge helper')
    sub = p.add_subparsers(dest='cmd', required=True)

    c = sub.add_parser('create')
    c.add_argument('--sender', required=True)
    c.add_argument('--recipient', required=True)
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
    c.add_argument('--idempotency-key')
    c.add_argument('--correlation-id')
    c.add_argument('--causation-id')
    c.set_defaults(func=create)

    s = sub.add_parser('status')
    s.add_argument('--actor', required=True)
    s.add_argument('handoff_id')
    s.set_defaults(func=status)

    l = sub.add_parser('list-open')
    l.add_argument('--agent', required=True)
    l.set_defaults(func=list_open)

    u = sub.add_parser('set-status')
    u.add_argument('--actor', required=True)
    u.add_argument('handoff_id')
    u.add_argument('status', choices=sorted(ALL_STATUSES))
    u.add_argument('--outcome', default='')
    u.add_argument('--expected-revision', type=int)
    u.set_defaults(func=set_status)

    a = sub.add_parser('archive')
    a.add_argument('--actor', required=True)
    a.add_argument('handoff_id')
    a.add_argument('--expected-revision', type=int)
    a.set_defaults(func=archive)

    ol = sub.add_parser('outbox-list')
    ol.add_argument('--state', action='append', choices=['pending', 'retrying', 'delivered', 'dead_letter'])
    ol.set_defaults(func=outbox_list)

    replay_parser = sub.add_parser("outbox-replay", help="Replay an event without changing its event ID")
    replay_parser.add_argument("event_id")
    replay_parser.set_defaults(func=outbox_replay)

    resolve_parser = sub.add_parser(
        "outbox-resolve-transaction",
        help="Explicitly enqueue or discard a prepared transaction with audit evidence",
    )
    resolve_parser.add_argument("event_id")
    resolve_parser.add_argument("--action", choices=("enqueue", "discard"), required=True)
    resolve_parser.add_argument("--reason", required=True)
    resolve_parser.add_argument(
        "--force-recoverable-discard",
        action="store_true",
        help="Permit discard even when canonical state still matches and automatic recovery is safe",
    )
    resolve_parser.set_defaults(func=outbox_resolve_transaction)

    rec = sub.add_parser('reconcile')
    rec.add_argument('handoff_id', nargs='?')
    rec.add_argument('--check-only', action='store_true')
    rec.set_defaults(func=reconcile)

    recover = sub.add_parser('outbox-recover')
    recover.set_defaults(func=recover_outbox)

    deliver = sub.add_parser('outbox-deliver')
    deliver.add_argument('--limit', type=int, default=100)
    deliver.add_argument('--timeout', type=float, default=2.0)
    deliver.add_argument('--max-attempts', type=int, default=int(os.environ.get('BRIDGE_OUTBOX_MAX_ATTEMPTS', '8')))
    deliver.add_argument('--base-delay-seconds', type=float, default=float(os.environ.get('BRIDGE_OUTBOX_BASE_DELAY_SECONDS', '1')))
    deliver.add_argument('--max-delay-seconds', type=float, default=float(os.environ.get('BRIDGE_OUTBOX_MAX_DELAY_SECONDS', '300')))
    deliver.set_defaults(func=deliver_outbox)

    args = p.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
