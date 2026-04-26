#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from bridge_core.runtime import normalize_agent_id
from bridge_wrapper_common import api_request, archive_result, create_result, invoke, list_result, set_status_result, status_result


def main() -> None:
    parser = argparse.ArgumentParser(description='Generic Bridge wrapper for any configured agent')
    parser.add_argument('--agent', required=True, help='agent identity used for API auth and CLI fallback')
    parser.add_argument('--allow-cli-fallback', action='store_true', help='allow direct bridge_cli fallback when the local API is unavailable')
    sub = parser.add_subparsers(dest='cmd', required=True)

    create_parser = sub.add_parser('create')
    create_parser.add_argument('--recipient', required=True)
    create_parser.add_argument('--issue-type', required=True)
    create_parser.add_argument('--handoff-kind', default='request', choices=['incident', 'request', 'question', 'result'])
    create_parser.add_argument('--priority', default='medium', choices=['low', 'medium', 'high', 'urgent'])
    create_parser.add_argument('--risk-level', default='low', choices=['low', 'medium', 'high'])
    create_parser.add_argument('--due-at', default='')
    create_parser.add_argument('--subject', required=True)
    create_parser.add_argument('--requested-action', required=True)
    create_parser.add_argument('--minimal-context', required=True)
    create_parser.add_argument('--constraints', default='')
    create_parser.add_argument('--response-format', default='concise status + action + blocker if any')
    create_parser.add_argument('--related-path', action='append')
    create_parser.add_argument('--approval-needed', action='store_true')
    create_parser.add_argument('--approval-context', default='')

    sub.add_parser('list-open')

    status_parser = sub.add_parser('status')
    status_parser.add_argument('handoff_id')

    update_parser = sub.add_parser('set-status')
    update_parser.add_argument('handoff_id')
    update_parser.add_argument('status', choices=['open', 'acknowledged', 'in_progress', 'blocked', 'closed'])
    update_parser.add_argument('--outcome', default='')

    ack_parser = sub.add_parser('ack')
    ack_parser.add_argument('handoff_id')

    block_parser = sub.add_parser('block')
    block_parser.add_argument('handoff_id')
    block_parser.add_argument('--outcome', required=True)

    close_parser = sub.add_parser('close')
    close_parser.add_argument('handoff_id')
    close_parser.add_argument('--outcome', required=True)

    archive_parser = sub.add_parser('archive')
    archive_parser.add_argument('handoff_id')

    args = parser.parse_args()
    agent = normalize_agent_id(args.agent)

    if args.cmd == 'create':
        payload = {
            'recipient': normalize_agent_id(args.recipient, field_name='recipient'),
            'issue_type': args.issue_type,
            'handoff_kind': args.handoff_kind,
            'priority': args.priority,
            'risk_level': args.risk_level,
            'due_at': args.due_at,
            'subject': args.subject,
            'requested_action': args.requested_action,
            'minimal_context': args.minimal_context,
            'constraints': args.constraints,
            'response_format': args.response_format,
            'related_paths': args.related_path or [],
            'approval_needed': args.approval_needed,
            'approval_context': args.approval_context,
        }
        cli_args = [
            'create', '--sender', agent, '--recipient', payload['recipient'], '--issue-type', args.issue_type,
            '--handoff-kind', args.handoff_kind, '--priority', args.priority, '--risk-level', args.risk_level,
            '--subject', args.subject, '--requested-action', args.requested_action,
            '--minimal-context', args.minimal_context, '--response-format', args.response_format,
        ]
        if args.constraints:
            cli_args += ['--constraints', args.constraints]
        if args.due_at:
            cli_args += ['--due-at', args.due_at]
        if args.approval_needed:
            cli_args.append('--approval-needed')
        if args.approval_context:
            cli_args += ['--approval-context', args.approval_context]
        for path in args.related_path or []:
            cli_args += ['--related-path', path]
        invoke(agent, args, api_call=lambda: create_result(api_request(agent, 'POST', '/v1/handoffs', payload=payload)), cli_args=cli_args)
    elif args.cmd == 'list-open':
        invoke(agent, args, api_call=lambda: list_result(agent, api_request(agent, 'GET', '/v1/handoffs?active_only=true')), cli_args=['list-open', '--agent', agent])
    elif args.cmd == 'status':
        invoke(agent, args, api_call=lambda: status_result(agent, args.handoff_id, api_request(agent, 'GET', f'/v1/handoffs/{args.handoff_id}')), cli_args=['status', '--actor', agent, args.handoff_id])
    elif args.cmd == 'set-status':
        payload = {'status': args.status, 'outcome': args.outcome}
        cli_args = ['set-status', '--actor', agent, args.handoff_id, args.status]
        if args.outcome:
            cli_args += ['--outcome', args.outcome]
        invoke(agent, args, api_call=lambda: set_status_result(agent, api_request(agent, 'POST', f'/v1/handoffs/{args.handoff_id}/status', payload=payload)), cli_args=cli_args)
    elif args.cmd == 'ack':
        invoke(agent, args, api_call=lambda: set_status_result(agent, api_request(agent, 'POST', f'/v1/handoffs/{args.handoff_id}/ack', payload={})), cli_args=['set-status', '--actor', agent, args.handoff_id, 'acknowledged'])
    elif args.cmd == 'block':
        invoke(agent, args, api_call=lambda: set_status_result(agent, api_request(agent, 'POST', f'/v1/handoffs/{args.handoff_id}/block', payload={'outcome': args.outcome})), cli_args=['set-status', '--actor', agent, args.handoff_id, 'blocked', '--outcome', args.outcome])
    elif args.cmd == 'close':
        invoke(agent, args, api_call=lambda: set_status_result(agent, api_request(agent, 'POST', f'/v1/handoffs/{args.handoff_id}/close', payload={'outcome': args.outcome})), cli_args=['set-status', '--actor', agent, args.handoff_id, 'closed', '--outcome', args.outcome])
    elif args.cmd == 'archive':
        invoke(agent, args, api_call=lambda: archive_result(agent, api_request(agent, 'POST', f'/v1/handoffs/{args.handoff_id}/archive', payload={})), cli_args=['archive', '--actor', agent, args.handoff_id])


if __name__ == '__main__':
    main()
