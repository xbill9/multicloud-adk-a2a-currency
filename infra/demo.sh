#!/usr/bin/env bash
# The whole thing, one command.
#
#   ./infra/demo.sh
#
# Four acts, in the order the claims depend on each other:
#
#   1. three native agents answer one question and agree
#   2. the 3x3 interop matrix -- every client SDK against every serving stack
#   3. a cloud goes offline and the median holds
#   4. a cloud lies and is named as the outlier
#
# Acts 3 and 4 are the point. Any demo can show three green ticks; the claim
# this project actually makes is about what happens when one participant is
# wrong, and that is only worth anything if you watch it happen.
#
# LOCAL, direct-brain. This is a protocol and consensus demo, not a
# cross-cloud measurement -- nothing here crosses a cloud boundary and the
# latencies are loopback. See docs/DEPLOYMENT_PLAN.md for what deployment adds.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
MESH="$REPO/infra/run_mesh.sh"

# ADK emits [EXPERIMENTAL] warnings on every call (finding 3) to stderr.
# Drop stderr rather than filtering stdout: the per-participant lines are
# indented, so any leading-whitespace filter eats the actual result.
run() { "$@" 2>/dev/null; }

rule() { printf '\n\033[1m%s\033[0m\n%s\n' "$1" "$(printf '─%.0s' $(seq 1 68))"; }

cleanup() { "$MESH" stop >/dev/null 2>&1 || true; }
trap cleanup EXIT

cleanup
rule "Starting three native agents"
"$MESH" start 2>&1 | grep -E 'starting|ready'
"$MESH" status

rule "1. Three clouds, one question"
echo "Google ADK, AWS Strands, Azure Agent Framework -- three vendors'"
echo "frameworks, three serving stacks, one answer."
echo
run "$PYTHON" -m coordinator.cli 100 USD EUR JPY

rule "2. The interop matrix"
echo "Three client SDKs x three natively-served agents. Every cell is a real"
echo "A2A call; a failure records which layer broke."
echo
run "$PYTHON" -m matrix.runner

rule "3. A cloud goes offline"
echo "Killing the AWS agent. The median means a lost participant degrades the"
echo "quorum instead of failing the run -- and the failure names its layer."
echo
"$MESH" kill aws
sleep 1
run "$PYTHON" -m coordinator.cli 100 USD EUR

rule "4. A cloud lies"
echo "Restarting AWS with a perturbed rate table. Consensus is the median, so"
echo "one divergent cloud cannot move the answer -- it is named as the outlier."
echo
"$MESH" stop >/dev/null 2>&1
CURRENCY_RATE_SCALE_AWS=1.35 "$MESH" start >/dev/null 2>&1
sleep 1
run "$PYTHON" -m coordinator.cli 100 USD EUR

rule "What this demo does not show"
cat <<'EOF'
  - Nothing in THIS SCRIPT is deployed. All three agents here are local and
    no measurement above crosses a cloud boundary -- but all three are also
    deployed for real, and acts 1 and 2 have hosted equivalents:
      ./infra/deploy_gcp.sh run       three clouds, one question
      ./infra/deploy_gcp.sh matrix    the 3x3 against hosted servers
      ./infra/deploy_gcp.sh verify    the negative controls
    Acts 3 and 4 stay local on purpose: killing and skewing a participant is
    something you can only do to an agent you own the process of.
  - Latencies are loopback and direct-brain (no model). They measure protocol
    and framework overhead, nothing else.
  - The three agents read the same fixture table, so agreement in act 1 is by
    construction. That is why act 4 exists.
EOF
echo
