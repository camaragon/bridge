# Approval Needed Example

```md
---
handoff_id: HND-YYYYMMDD-HHMMSS-xxx
status: blocked
created_at: YYYY-MM-DDTHH:MM:SSZ
updated_at: YYYY-MM-DDTHH:MM:SSZ
sender: agent-a
recipient: agent-b
issue_type: approval_needed
handoff_kind: request
priority: high
risk_level: high
due_at: none
approval_needed: yes
approval_context: Human approval required before action.
resolution_summary: pending
subject: Short approval request
response_format: explicit approve/deny + optional constraints
related_paths:
  - /path/if/relevant
---

## Decision Needed

## Requested Action
- decide whether to approve one of options below

## Why Approval Is Required

## Minimal Context

## Constraints
- do not proceed until approval arrives

## Approval Needed

## Options
- option A
- option B

## Recommended Option
- one line

## Outcome
- pending
```
