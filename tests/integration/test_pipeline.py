"""End-to-end runs of the real pipeline: real git worktrees, real uv environments
(pydantic 1.x baseline and 2.x target), real sandboxed pytest + probe executions.

No LLM is used here: agents are scripted (a known-good fixer, red-team adversaries, a
cost-burning agent) so the harness behaviour is deterministic and free to test.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from migrationguard.agents.base import AgentTask
from migrationguard.agents.scripted import ADVERSARIES, ScriptedAgent, adversary
from migrationguard.harness.orchestrator import Orchestrator, RunOptions
from migrationguard.harness.store import Store
from migrationguard.models import AgentResult, AgentUsage, Phase, VerificationResult
from tests.conftest import FIXTURES, ROOT, make_repo, uv_available

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not uv_available(), reason="uv not available")]

SANDBOX = os.environ.get("MG_TEST_SANDBOX", "auto")


def gates(state) -> dict[str, str]:
    path = Path(state.run_dir) / "artifacts" / state.last_verification
    return {g.gate: g.status for g in VerificationResult.model_validate_json(path.read_text()).gates}


def fast_retries(repo: Path) -> None:
    config = repo / "migrationguard.toml"
    text = config.read_text() if config.exists() else ""
    config.write_text(text + "\n[retry]\nmax_attempts = 3\nbase_delay_seconds = 0.01\nmax_delay_seconds = 0.02\n")
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "fast retries"], cwd=repo, check=True)


def billing_fixer() -> ScriptedAgent:
    """Writes a known-good migration of billing/models.py *through the policy-checked toolbox*."""

    def repair(task: AgentTask) -> str:
        result = task.toolbox.write_file("billing/models.py", (FIXTURES / "billing_models_v2.py").read_text())
        assert result.startswith("wrote"), result
        return "restored v1 coercion and Decimal JSON encoding"

    return ScriptedAgent("fixer", repair, claims=["All tests pass", "No behaviour changes"])


# --------------------------------------------------------------------------- acceptance / rejection
def test_inventory_deterministic_migration_is_accepted(tmp_path, mg_home):
    repo = make_repo(tmp_path, "inventory")
    state = Orchestrator(home=mg_home).start(repo, RunOptions(agent="none", sandbox=SANDBOX))
    assert state.phase is Phase.ACCEPTED, state.decision
    g = gates(state)
    assert all(status in ("pass", "warn") for status in g.values()), g
    assert g["behavior.probes"] == "warn"  # differences exist but were accepted by humans in migrationguard.toml
    # the user's checkout is untouched; the result lives on the run branch
    assert "pydantic>=1.10,<2" in (repo / "requirements.txt").read_text()
    migrated = subprocess.run(["git", "show", f"{state.branch}:inventory/models.py"], cwd=repo, capture_output=True, text=True).stdout
    assert "model_config = ConfigDict(" in migrated and "class Config" not in migrated
    report = (Path(state.run_dir) / "report.md").read_text()
    assert "ACCEPTED" in report and "accepted: v1 silently truncated" in report


def test_billing_codemod_regression_is_caught_by_differential_probes(tmp_path, mg_home):
    """Codemods alone keep every visible test green but silently change behaviour."""
    repo = make_repo(tmp_path, "billing")
    state = Orchestrator(home=mg_home).start(repo, RunOptions(agent="none", sandbox=SANDBOX))
    assert state.phase is Phase.REJECTED
    g = gates(state)
    assert g["tests.pass"] == "pass" and g["tests.differential"] == "pass"
    assert g["behavior.probes"] == "fail"
    assert state.decision.blocking_gates == ["behavior.probes"]


def test_deliberately_injected_regression_is_detected(tmp_path, mg_home):
    """Inject a subtle behaviour change after the codemod commit: a default value change that no
    visible test exercises. The differential probes must catch it and reject the migration."""
    repo = make_repo(tmp_path, "inventory")
    injected = {"done": False}

    def inject(phase: Phase, state) -> None:
        if phase is Phase.VERIFY and not injected["done"]:
            injected["done"] = True
            target = Path(state.run_dir) / "worktrees" / "target"
            models = target / "inventory" / "models.py"
            models.write_text(models.read_text().replace('country: str = "US"', 'country: str = "CA"'))
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "regression"], cwd=target, check=True)

    state = Orchestrator(home=mg_home, fault_injector=inject).start(repo, RunOptions(agent="none", sandbox=SANDBOX))
    assert injected["done"]
    assert state.phase is Phase.REJECTED
    g = gates(state)
    assert g["tests.pass"] == "pass" and g["tests.differential"] == "pass"  # the test suite does not notice
    assert g["behavior.probes"] == "fail"
    path = Path(state.run_dir) / "artifacts" / state.last_verification
    probes = next(x for x in VerificationResult.model_validate_json(path.read_text()).gates if x.gate == "behavior.probes")
    assert any(d["kind"] == "value" and "Supplier" in d["probe_id"] for d in probes.details["diffs"])


def test_repair_loop_with_policy_checked_agent_is_accepted(tmp_path, mg_home):
    repo = make_repo(tmp_path, "billing")
    orch = Orchestrator(home=mg_home, agent_factory=lambda config, events: billing_fixer())
    state = orch.start(repo, RunOptions(sandbox=SANDBOX, reviewer=True))
    assert state.phase is Phase.ACCEPTED, state.decision
    assert [h.passed for h in state.history] == [False, True]
    assert state.repair_attempts == 1
    report = (Path(state.run_dir) / "report.md").read_text()
    assert "✅ supported" in report  # the fixer's claims were checked against evidence


# --------------------------------------------------------------------------- red team
EXPECTED_CATCH = {
    "liar": {"tests.pass", "tests.differential"},
    "delete-tests": {"tests.integrity", "tests.differential"},
    "skip-tests": {"tests.integrity", "tests.differential"},
    "weaken-asserts": {"tests.integrity"},
    "conftest-deselect": {"tests.integrity", "tests.differential"},
    "pin-v1": {"deps.target", "static.residual"},
    "v1-shim": {"static.residual", "tests.integrity"},
    "detect-pytest": {"tests.integrity"},
}


@pytest.mark.parametrize("name", sorted(ADVERSARIES))
def test_red_team_agents_are_rejected(tmp_path, mg_home, name):
    repo = make_repo(tmp_path, "settings_service")  # codemods leave failing tests -> agent is invoked
    orch = Orchestrator(home=mg_home, agent_factory=lambda config, events: adversary(name))
    state = orch.start(repo, RunOptions(sandbox=SANDBOX, max_repair_iterations=1, reviewer=True))
    assert state.agent_calls >= 1
    assert state.phase is Phase.REJECTED, f"{name} was accepted!"
    failing = set(state.decision.blocking_gates)
    assert EXPECTED_CATCH[name] & failing, f"{name}: failing gates {failing}"


# --------------------------------------------------------------------------- fault tolerance
def test_stop_after_and_resume(tmp_path, mg_home):
    repo = make_repo(tmp_path, "inventory")
    orch = Orchestrator(home=mg_home)
    paused = orch.start(repo, RunOptions(agent="none", sandbox=SANDBOX, stop_after=Phase.CODEMOD))
    assert paused.phase is Phase.PAUSED and paused.paused_from is Phase.VERIFY
    resumed = Orchestrator(home=mg_home).resume(paused.run_id)
    assert resumed.phase is Phase.ACCEPTED
    phases = [c["phase"] for c in Store(mg_home / "migrationguard.db").checkpoints(paused.run_id)]
    # ... codemod -> verify (transition checkpoint) -> paused | resumed: -> review/decide -> accepted
    assert phases[:7] == ["init", "analyze", "baseline", "plan", "codemod", "verify", "paused"]
    assert phases[-2:] == ["decide", "accepted"]


def test_hard_crash_then_resume(tmp_path, mg_home):
    """Kill the process (os._exit) in the middle of the run; resume from the checkpoint."""
    repo = make_repo(tmp_path, "inventory")
    env = {**os.environ, "MIGRATIONGUARD_HOME": str(mg_home), "MIGRATIONGUARD_FAULT": "verify:crash"}
    cli = [sys.executable, "-m", "migrationguard.cli"]
    crashed = subprocess.run([*cli, "run", str(repo), "--agent", "none", "--sandbox", SANDBOX], env=env,
                             capture_output=True, text=True, timeout=900)
    assert crashed.returncode == 137, crashed.stdout[-2000:] + crashed.stderr[-2000:]
    store = Store(mg_home / "migrationguard.db")
    run = store.list_runs(1)[0]
    assert run["phase"] == "verify"  # checkpointed, not terminal
    env.pop("MIGRATIONGUARD_FAULT")
    resumed = subprocess.run([*cli, "resume", run["id"]], env=env, capture_output=True, text=True, timeout=900)
    assert resumed.returncode == 0, resumed.stdout[-3000:] + resumed.stderr[-2000:]
    assert store.get_run(run["id"])["phase"] == "accepted"
    assert any(e["kind"] == "run.resume" for e in store.events(run["id"]))


def test_transient_failures_are_retried(tmp_path, mg_home):
    repo = make_repo(tmp_path, "inventory")
    fast_retries(repo)
    os.environ["MIGRATIONGUARD_FAULT"] = "baseline:transient:2"
    try:
        state = Orchestrator(home=mg_home).start(repo, RunOptions(agent="none", sandbox=SANDBOX))
    finally:
        os.environ.pop("MIGRATIONGUARD_FAULT")
    assert state.phase is Phase.ACCEPTED
    assert state.phase_retries == {"baseline": 2}


def test_interrupt_during_repair_then_resume(tmp_path, mg_home):
    repo = make_repo(tmp_path, "billing")
    os.environ["MIGRATIONGUARD_FAULT"] = "repair:interrupt"
    try:
        paused = Orchestrator(home=mg_home, agent_factory=lambda c, e: billing_fixer()).start(repo, RunOptions(sandbox=SANDBOX))
    finally:
        os.environ.pop("MIGRATIONGUARD_FAULT")
    assert paused.phase is Phase.PAUSED and paused.paused_from is Phase.REPAIR
    resumed = Orchestrator(home=mg_home, agent_factory=lambda c, e: billing_fixer()).resume(paused.run_id)
    assert resumed.phase is Phase.ACCEPTED


class CostlyAgent:
    """Edits a comment and reports a large spend: exercises budget enforcement."""

    name = "costly"

    def repair(self, task: AgentTask) -> AgentResult:
        path = task.worktree / "orders" / "config.py"
        path.write_text(path.read_text() + f"\n# attempt {task.iteration}\n")
        return AgentResult(role="repair", status="finished", summary="tried", usage=AgentUsage(cost_usd=5.0, output_tokens=1000))

    def review(self, task: AgentTask) -> AgentResult:  # pragma: no cover - never reached
        raise AssertionError


def test_budget_exhaustion_pauses_and_resume_with_more_budget(tmp_path, mg_home):
    repo = make_repo(tmp_path, "settings_service")
    factory = lambda config, events: CostlyAgent()  # noqa: E731
    paused = Orchestrator(home=mg_home, agent_factory=factory).start(
        repo, RunOptions(sandbox=SANDBOX, budget_usd=4.0, max_repair_iterations=2))
    assert paused.phase is Phase.PAUSED and "cost_usd" in paused.pause_reason
    assert paused.repair_attempts == 1 and paused.usage.cost_usd == 5.0
    final = Orchestrator(home=mg_home, agent_factory=factory).resume(paused.run_id, RunOptions(budget_usd=50.0))
    assert final.phase is Phase.REJECTED  # the agent never fixes anything
    assert final.repair_attempts == 2 and final.usage.cost_usd == 10.0


# --------------------------------------------------------------------------- known limitation + remedy
def webhooks_naive_fixer() -> ScriptedAgent:
    """Fixes everything the visible tests and model probes can see — but not the raw JSON
    separators of `.json()` → `model_dump_json()` (the signed payload string changes)."""

    def repair(task: AgentTask) -> str:
        result = task.toolbox.write_file("webhooks/events.py", (FIXTURES / "webhooks_events_v2_naive.py").read_text())
        assert result.startswith("wrote"), result
        return "migrated events to v2"

    return ScriptedAgent("naive-webhooks", repair)


def test_raw_json_format_change_is_invisible_to_model_probes(tmp_path, mg_home):
    """Documents a real limitation: built-in probes compare *parsed* JSON, so a change of
    separators in a signed payload passes every built-in gate (the hidden oracle disagrees)."""
    repo = make_repo(tmp_path, "webhooks")
    orch = Orchestrator(home=mg_home, agent_factory=lambda c, e: webhooks_naive_fixer())
    state = orch.start(repo, RunOptions(sandbox=SANDBOX, reviewer=False))
    assert state.phase is Phase.ACCEPTED, state.decision


def test_user_probe_catches_raw_json_format_change(tmp_path, mg_home):
    repo = make_repo(tmp_path, "webhooks")
    (repo / "migrationguard_probes.py").write_text((ROOT / "examples" / "user-probes" / "migrationguard_probes.py").read_text())
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "add probes"], cwd=repo, check=True)
    orch = Orchestrator(home=mg_home, agent_factory=lambda c, e: webhooks_naive_fixer())
    state = orch.start(repo, RunOptions(sandbox=SANDBOX, reviewer=False, max_repair_iterations=1))
    assert state.phase is Phase.REJECTED
    assert state.decision.blocking_gates == ["behavior.probes"]
    path = Path(state.run_dir) / "artifacts" / state.last_verification
    probes = next(g for g in VerificationResult.model_validate_json(path.read_text()).gates if g.gate == "behavior.probes")
    diff = next(d for d in probes.details["diffs"] if d["probe_id"] == "user:probe_signing_payload")
    assert ", " in diff["baseline"] and ", " not in diff["target"]


def test_persistent_transient_failure_pauses_instead_of_failing(tmp_path, mg_home):
    repo = make_repo(tmp_path, "inventory")
    fast_retries(repo)
    os.environ["MIGRATIONGUARD_FAULT"] = "baseline:transient:3"  # one more than max_attempts
    try:
        paused = Orchestrator(home=mg_home).start(repo, RunOptions(agent="none", sandbox=SANDBOX))
    finally:
        os.environ.pop("MIGRATIONGUARD_FAULT")
    assert paused.phase is Phase.PAUSED and "transient failure persisted" in paused.pause_reason
    assert Orchestrator(home=mg_home).resume(paused.run_id).phase is Phase.ACCEPTED


class QuotaAgent:
    """Simulates the provider's usage limit on the first call (as observed in a real run)."""

    name = "quota"
    calls = 0

    def repair(self, task: AgentTask) -> AgentResult:
        QuotaAgent.calls += 1
        if QuotaAgent.calls == 1:
            from migrationguard.agents.claude_sdk import classify_sdk_error

            classify_sdk_error(RuntimeError("Claude Code returned an error result: You've hit your session limit · resets 5:50am"))
        return billing_fixer().repair(task)

    def review(self, task: AgentTask) -> AgentResult:
        return billing_fixer().review(task)


def test_provider_quota_pauses_and_resume_completes(tmp_path, mg_home):
    repo = make_repo(tmp_path, "billing")
    QuotaAgent.calls = 0
    paused = Orchestrator(home=mg_home, agent_factory=lambda c, e: QuotaAgent()).start(repo, RunOptions(sandbox=SANDBOX))
    assert paused.phase is Phase.PAUSED and "usage limit" in paused.pause_reason
    assert QuotaAgent.calls == 1  # not retried: retrying a quota error is pointless
    resumed = Orchestrator(home=mg_home, agent_factory=lambda c, e: QuotaAgent()).resume(paused.run_id)
    assert resumed.phase is Phase.ACCEPTED
