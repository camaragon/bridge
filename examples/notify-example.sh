#!/bin/sh
# Customize /usr/local/bin/agent-wake to your runtime's supported wake adapter.
# That site-provided executable accepts --agent ID and a single pointer argument.
# It must be callable by the writer; Bridge does not change Unix accounts.
set -eu
if [ "${1:-}" = --unseen ]; then
    agent=${2:?expected agent id}
    pending=$(bridge unseen --as "$agent")
    [ -n "$pending" ] || exit 0
    pointer="Bridge has unseen events; run bridge unseen --as $agent, handle them, and mark the returned snapshot."
else
    agent=${1:?expected agent id}
    IFS= read -r pointer
fi
# Do not mark here: the receiving agent must read the events through Bridge.
exec /usr/local/bin/agent-wake --agent "$agent" "$pointer"
