#!/usr/bin/env bash
# Run by collector/run_loop.py ~10 min before a collect.yml run hits its cap: dispatches
# the successor that keeps the 24/7 chain going.
#
# Convergence rule: if a NEWER chain run started via workflow_dispatch is already queued or
# in progress (a second chain, e.g. from two scheduled kickoffs racing, or a manual
# `-f mode=chain`), this chain stops here instead - so any number of parallel chains
# converges to one within a hop. Newest-wins means the two can never both stop. Scheduled
# runs are ignored here: a scheduled kickoff that's about to skip itself because this
# chain is alive must not also make this chain stop.
#
# Env (set by collect.yml): GH_TOKEN, GH_REPO, GITHUB_RUN_ID, GITHUB_REF_NAME, HOPS
# (-1 = unlimited, else successors this run may still dispatch), MAX_MINUTES.
set -euo pipefail

newer=$(gh run list --workflow collect.yml --limit 30 --json databaseId,status,event,displayTitle -q "
  [.[] | select(.databaseId > $GITHUB_RUN_ID and .event == \"workflow_dispatch\"
                and (.status == \"in_progress\" or .status == \"queued\")
                and (.displayTitle | contains(\"chain\")))] | length")
if [ "$newer" -gt 0 ]; then
  echo "a newer chain run is already active - letting this chain end here"
  exit 0
fi

next_hops=$([ "$HOPS" -lt 0 ] && echo -1 || echo $((HOPS - 1)))
gh workflow run collect.yml --ref "$GITHUB_REF_NAME" \
  -f mode=chain -f hops="$next_hops" -f max_minutes="$MAX_MINUTES"
