#!/usr/bin/env python3
import argparse

from bridge_wrapper_common import api_request, archive_result, create_result, invoke, list_result, set_status_result, status_result

AGENT = 'jarvy'
ALLOWED_RECIPIENTS = {'hermes'}


def main():
    p = argparse.ArgumentParser(description='Bridge wrapper for jarvy')
    p.add_argument('--allow-cli-fallback', action='store_true', help='allow direct bridge_cli fallback when the local API is unavailable')
    sub = p.add_subparsers(dest='cmd', required=True)

    c = sub.add_parser('create')
    c.add_argument('--recipient', required=True, choices=sorted(ALLOWED_RECIPIENTS))
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

    sub.add_parser('list-open')

    s = sub.add_parser('status')
    s.add_argument('handoff_id')

    u = sub.add_parser('set-status')
    u.add_argument('handoff_id')
    u.add_argument('status', choices=['open', 'acknowledged', 'in_progress', 'blocked', 'closed'])
    u.add_argument('--outcome', default='')

    k = sub.add_parser('ack')
    k.add_argument('handoff_id')

    b = sub.add_parser('block')
    b.add_argument('handoff_id')
    b.add_argument('--outcome', required=True)

    cl = sub.add_parser('close')
    cl.add_argument('handoff_id')
    cl.add_argument('--outcome', required=True)

    a = sub.add_parser('archive')
    a.add_argument('handoff_id')

    args = p.parse_args()

    if args.cmd == 'create':
        payload = {
            'recipient': args.recipient,
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
            'create', '--sender', AGENT, '--recipient', args.recipient, '--issue-type', args.issue_type,
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
        invoke(AGENT, args, api_call=lambda: create_result(api_request(AGENT, 'POST', '/v1/handoffs', payload=payload)), cli_args=cli_args)
    elif args.cmd == 'list-open':
        invoke(AGENT, args, api_call=lambda: list_result(AGENT, api_request(AGENT, 'GET', '/v1/handoffs?active_only=true')), cli_args=['list-open', '--agent', AGENT])
    elif args.cmd == 'status':
        invoke(AGENT, args, api_call=lambda: status_result(AGENT, args.handoff_id, api_request(AGENT, 'GET', f'/v1/handoffs/{args.handoff_id}')), cli_args=['status', '--actor', AGENT, args.handoff_id])
    elif args.cmd == 'set-status':
        payload = {'status': args.status, 'outcome': args.outcome}
        cli_args = ['set-status', '--actor', AGENT, args.handoff_id, args.status]
        if args.outcome:
            cli_args += ['--outcome', args.outcome]
        invoke(AGENT, args, api_call=lambda: set_status_result(AGENT, api_request(AGENT, 'POST', f'/v1/handoffs/{args.handoff_id}/status', payload=payload)), cli_args=cli_args)
    elif args.cmd == 'ack':
        invoke(AGENT, args, api_call=lambda: set_status_result(AGENT, api_request(AGENT, 'POST', f'/v1/handoffs/{args.handoff_id}/ack', payload={})), cli_args=['set-status', '--actor', AGENT, args.handoff_id, 'acknowledged'])
    elif args.cmd == 'block':
        invoke(AGENT, args, api_call=lambda: set_status_result(AGENT, api_request(AGENT, 'POST', f'/v1/handoffs/{args.handoff_id}/block', payload={'outcome': args.outcome})), cli_args=['set-status', '--actor', AGENT, args.handoff_id, 'blocked', '--outcome', args.outcome])
    elif args.cmd == 'close':
        invoke(AGENT, args, api_call=lambda: set_status_result(AGENT, api_request(AGENT, 'POST', f'/v1/handoffs/{args.handoff_id}/close', payload={'outcome': args.outcome})), cli_args=['set-status', '--actor', AGENT, args.handoff_id, 'closed', '--outcome', args.outcome])
    elif args.cmd == 'archive':
        invoke(AGENT, args, api_call=lambda: archive_result(AGENT, api_request(AGENT, 'POST', f'/v1/handoffs/{args.handoff_id}/archive', payload={})), cli_args=['archive', '--actor', AGENT, args.handoff_id])


if __name__ == '__main__':
    main()
