"""State machine, budget, retries, SQLite store, event log, report claim checking."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from migrationguard.config import BudgetSection, MigrationGuardConfig, RetrySection, deep_merge
from migrationguard.errors import BudgetExceeded, InvalidTransition, TransientError
from migrationguard.harness.events import EventLog
from migrationguard.harness.state import BudgetTracker, RunState, check_transition, with_retries
from migrationguard.harness.store import Store
from migrationguard.models import AgentClaim, AgentResult, AgentUsage, GateResult, Phase
from migrationguard.report.render import check_claim


def state(**kw) -> RunState:
    data = {"run_id": "r1", "repo": "/r", "run_dir": "/d", "branch": "b", "base_commit": "c", **kw}
    return RunState(**data)


def test_happy_path_transitions():
    s = state()
    for phase in (Phase.ANALYZE, Phase.BASELINE, Phase.PLAN, Phase.CODEMOD, Phase.VERIFY, Phase.REPAIR,
                  Phase.VERIFY, Phase.REVIEW, Phase.DECIDE, Phase.ACCEPTED):
        s.transition(phase)
    assert s.phase is Phase.ACCEPTED


@pytest.mark.parametrize("src, dst", [
    (Phase.INIT, Phase.VERIFY), (Phase.CODEMOD, Phase.ACCEPTED), (Phase.VERIFY, Phase.ACCEPTED),
    (Phase.ACCEPTED, Phase.ANALYZE), (Phase.PAUSED, Phase.VERIFY),
])
def test_illegal_transitions(src, dst):
    with pytest.raises(InvalidTransition):
        check_transition(src, dst)


def test_any_phase_can_pause_or_fail():
    s = state(phase=Phase.REPAIR)
    s.transition(Phase.PAUSED)
    assert s.paused_from is Phase.REPAIR
    check_transition(Phase.BASELINE, Phase.FAILED)


def test_budget_tracker():
    s = state()
    tracker = BudgetTracker(BudgetSection(max_cost_usd=1.0, max_tokens=1000, max_agent_calls=5), s)
    tracker.check()
    tracker.record(AgentUsage(cost_usd=0.6, input_tokens=10))
    tracker.check()
    assert tracker.remaining_usd() == pytest.approx(0.4)
    tracker.record(AgentUsage(cost_usd=0.5))
    with pytest.raises(BudgetExceeded) as exc:
        tracker.check()
    assert exc.value.what == "cost_usd"
    s2 = state()
    t2 = BudgetTracker(BudgetSection(max_agent_calls=1), s2)
    t2.record(AgentUsage())
    with pytest.raises(BudgetExceeded, match="agent_calls"):
        t2.check()


def test_with_retries_retries_transient_only():
    calls = {"n": 0}
    delays = []

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TransientError("boom")
        return "ok"

    policy = RetrySection(max_attempts=3, base_delay_seconds=1, max_delay_seconds=2)
    assert with_retries(flaky, policy, sleep=delays.append) == "ok"
    assert calls["n"] == 3 and len(delays) == 2 and all(0.5 <= d <= 2 for d in delays)

    with pytest.raises(TransientError):
        with_retries(lambda: (_ for _ in ()).throw(TransientError("x")), policy, sleep=lambda d: None)
    with pytest.raises(ValueError):
        with_retries(lambda: (_ for _ in ()).throw(ValueError("x")), policy, sleep=lambda d: None)


def test_store_checkpoints_events_attempts(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.create_run("run-1", "/repo", "b", "c0", str(tmp_path), "init")
    s = state(run_id="run-1")
    assert store.checkpoint("run-1", "init", None, s.model_dump_json()) == 1
    s.phase = Phase.ANALYZE
    assert store.checkpoint("run-1", "analyze", "abc", s.model_dump_json()) == 2
    latest = store.latest_checkpoint("run-1")
    assert latest["seq"] == 2 and RunState.model_validate_json(latest["state_json"]).phase is Phase.ANALYZE
    assert store.get_run("run")["phase"] == "analyze"  # prefix lookup
    attempt = store.start_attempt("run-1", 1, "repair")
    assert store.mark_interrupted_attempts("run-1") == 1
    store.finish_attempt(attempt, AgentResult(role="repair", status="finished", claims=[AgentClaim(text="c")]), "finished", "sha")
    assert json.loads(store.attempts("run-1")[0]["claims_json"])[0]["text"] == "c"
    store.add_usage("run-1", "repair", "repair", None, AgentUsage(cost_usd=0.25, output_tokens=7))
    store.add_usage("run-1", "review", "reviewer", None, AgentUsage(cost_usd=0.5, input_tokens=3))
    total = store.usage_total("run-1")
    assert total.cost_usd == pytest.approx(0.75) and total.total_tokens == 10
    store.add_evidence("run-1", 1, "sha", [GateResult(gate="g", title="t", status="pass", summary="s")])
    assert store.evidence("run-1", 1)[0]["gate"] == "g"


def test_event_log_writes_db_jsonl_and_survives_bad_sink(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    seen = []

    def sink(event):
        seen.append(event)
        raise RuntimeError("console broke")

    log = EventLog(store, "r", tmp_path / "events.jsonl", sink)
    log.phase = "verify"
    log.emit("gate", "static.syntax: PASS", status="pass")
    assert seen and store.events("r")[0]["phase"] == "verify"
    assert json.loads((tmp_path / "events.jsonl").read_text())["data"] == {"status": "pass"}


def test_claim_checking():
    gates = {
        "tests.pass": GateResult(gate="tests.pass", title="", status="pass", summary=""),
        "tests.differential": GateResult(gate="tests.differential", title="", status="pass", summary=""),
        "tests.integrity": GateResult(gate="tests.integrity", title="", status="fail", summary=""),
        "behavior.probes": GateResult(gate="behavior.probes", title="", status="pass", summary=""),
    }
    assert check_claim("All tests pass", gates)[0] is False  # integrity failed
    assert check_claim("No behaviour changes", gates)[0] is True
    assert check_claim("I refactored nicely", gates)[0] is None


def test_config_load_and_overrides(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[tool.migrationguard.agent]\nmax_repair_iterations = 7\n')
    config = MigrationGuardConfig.load(tmp_path, {"budget": {"max_cost_usd": 2.5, "max_tokens": None}, "verify": {}})
    assert config.agent.max_repair_iterations == 7
    assert config.budget.max_cost_usd == 2.5 and config.budget.max_tokens == 4_000_000
    (tmp_path / "migrationguard.toml").write_text('[sandbox]\nbackend = "local"\n')
    assert MigrationGuardConfig.load(tmp_path).sandbox.backend == "local"
    with pytest.raises(ValidationError):
        MigrationGuardConfig.model_validate({"environment": {"python": "three"}})
    assert deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"b": None, "c": 3}}) == {"a": {"b": 1, "c": 3}}


def test_sdk_error_classification():
    from migrationguard.agents.claude_sdk import classify_sdk_error
    from migrationguard.errors import AgentQuotaExceeded

    with pytest.raises(AgentQuotaExceeded):
        classify_sdk_error(RuntimeError("Claude Code returned an error result: You've hit your session limit · resets 5:50am"))
    with pytest.raises(TransientError):
        classify_sdk_error(RuntimeError("API Error: 529 overloaded"))
    classify_sdk_error(ValueError("bad tool schema"))  # unknown errors are left to the caller
