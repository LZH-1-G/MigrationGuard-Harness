"""Live end-to-end migration with the real Claude Agent SDK.

Costs real money. Enable with ``MIGRATIONGUARD_LIVE_TESTS=1`` (needs Claude credentials).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from migrationguard.bench.cases import load_cases
from migrationguard.bench.oracle import evaluate
from migrationguard.config import MigrationGuardConfig
from migrationguard.harness.orchestrator import Orchestrator, RunOptions
from migrationguard.models import Phase
from migrationguard.sandbox.envs import EnvironmentProvisioner
from migrationguard.sandbox.runner import SandboxRunner, make_sandbox
from tests.conftest import make_repo

pytestmark = [
    pytest.mark.live,
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("MIGRATIONGUARD_LIVE_TESTS") != "1", reason="set MIGRATIONGUARD_LIVE_TESTS=1"),
]


@pytest.mark.parametrize("case", ["billing"])
def test_claude_migration_is_accepted_and_correct(tmp_path, mg_home, case):
    repo = make_repo(tmp_path, case)
    state = Orchestrator(home=mg_home).start(repo, RunOptions(agent="claude", budget_usd=6.0, max_repair_iterations=3))
    assert state.phase is Phase.ACCEPTED, state.decision
    assert state.agent_calls >= 1 and state.usage.cost_usd > 0
    run = Path(state.run_dir)
    config = MigrationGuardConfig.model_validate_json((run / "config.json").read_text())
    truth = evaluate(load_cases([case])[0], run / "worktrees" / "target", run / "worktrees" / "baseline", config,
                     EnvironmentProvisioner(mg_home / "envs"), SandboxRunner(make_sandbox(config), config), tmp_path / "gt")
    assert truth.correct, truth
