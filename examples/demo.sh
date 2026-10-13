#!/usr/bin/env bash
# MigrationGuard demo: migrate the billing benchmark service in a scratch directory.
#   ./examples/demo.sh            deterministic codemods + verification (no API key needed)
#   ./examples/demo.sh --claude   with the Claude repair and reviewer agents
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
CASE="${CASE:-billing}"
WORK="${WORK:-$(mktemp -d)/$CASE}"
AGENT="none"
[[ "${1:-}" == "--claude" ]] && AGENT="claude"

cp -r "$HERE/benchmarks/cases/$CASE/repo" "$WORK"
cd "$WORK"
git init -q -b main && git add -A && git -c user.name=demo -c user.email=demo@example.com commit -qm "$CASE (pydantic v1)"
echo "== project copied to $WORK"

echo; echo "== 1. analyze (static, executes nothing)"
migrationguard analyze .

echo; echo "== 2. run (agent: $AGENT)"
set +e
migrationguard run . --agent "$AGENT" --budget 5
code=$?
set -e
echo "exit code: $code (0 accepted, 1 rejected, 2 failed, 3 paused)"

RUN_ID="$(migrationguard status -q --limit 1)"
echo; echo "== 3. status $RUN_ID"
migrationguard status "$RUN_ID"
echo; echo "== 4. report"
migrationguard report "$RUN_ID"
