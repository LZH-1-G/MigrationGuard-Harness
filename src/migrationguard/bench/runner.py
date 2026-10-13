"""Benchmark runner: executes (case × strategy) and scores against ground truth."""

from __future__ import annotations

import shutil
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from migrationguard import __version__
from migrationguard.agents.scripted import adversary
from migrationguard.artifacts import ArtifactStore
from migrationguard.bench import strategies as strat_mod
from migrationguard.bench.cases import BenchCase, load_cases
from migrationguard.bench.oracle import evaluate
from migrationguard.config import MigrationGuardConfig, home_dir
from migrationguard.harness.orchestrator import Orchestrator, RunOptions
from migrationguard.models import Phase, VerificationResult, utcnow
from migrationguard.sandbox.envs import EnvironmentProvisioner
from migrationguard.sandbox.git import GitRepo
from migrationguard.sandbox.runner import SandboxRunner, make_sandbox


class BenchRow(BaseModel):
    case: str
    strategy: str
    run_id: str = ""
    outcome: str
    accepted: bool
    accepted_if_tests_only: bool = False
    behavior_ok: bool = False
    complete: bool = False
    correct: bool = False
    agent_invoked: bool = False
    iterations: int = 0
    repair_attempts: int = 0
    agent_calls: int = 0
    cost_usd: float = 0.0
    tokens: int = 0
    seconds: float = 0.0
    failing_gates: list[str] = Field(default_factory=list)
    gate_status: dict[str, str] = Field(default_factory=dict)
    truth_failures: list[str] = Field(default_factory=list)
    incomplete_reasons: list[str] = Field(default_factory=list)
    error: str = ""


class BenchResults(BaseModel):
    version: str = __version__
    started_at: str = Field(default_factory=lambda: utcnow().isoformat())
    environment: dict[str, Any] = Field(default_factory=dict)
    rows: list[BenchRow] = Field(default_factory=list)


@dataclass
class BenchConfig:
    cases: list[str] | None = None
    strategies: list[str] | None = None
    out: Path = Path("bench-results")
    sandbox: str = "auto"
    budget_usd: float = 5.0
    model: str | None = None
    verbose: bool = False
    home: Path | None = None


def _agent_factory(strategy: strat_mod.Strategy):
    if strategy.agent.startswith("adversary:"):
        name = strategy.agent.split(":", 1)[1]
        return lambda config, events: adversary(name)
    return None


def run_one(case: BenchCase, strategy: strat_mod.Strategy, cfg: BenchConfig, log: Callable[[str], None]) -> BenchRow:
    work = (cfg.out / "work" / f"{case.name}--{strategy.name}").resolve()
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(case.repo, work, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"))
    GitRepo.init(work, message=f"benchmark case {case.name}")
    home = cfg.home or home_dir()

    def sink(event: dict[str, Any]) -> None:
        if cfg.verbose and event["level"] != "debug":
            log(f"    [{event.get('phase') or '-'}] {event['message'][:200]}")

    orch = Orchestrator(home=home, sink=sink, agent_factory=_agent_factory(strategy))
    agent = "claude" if strategy.agent == "claude" else ("none" if strategy.agent == "none" else None)
    options = RunOptions(
        agent=agent, sandbox=cfg.sandbox, model=cfg.model, max_repair_iterations=strategy.max_repairs,
        budget_usd=cfg.budget_usd, reviewer=strategy.reviewer, probes=strategy.probes, codemods=strategy.codemods,
        extra_overrides=strategy.overrides(),
    )
    started = time.monotonic()
    try:
        state = orch.start(work, options)
    except Exception as exc:
        return BenchRow(case=case.name, strategy=strategy.name, outcome="error", accepted=False, error=str(exc)[:500],
                        seconds=round(time.monotonic() - started, 1))
    seconds = time.monotonic() - started
    row = BenchRow(
        case=case.name, strategy=strategy.name, run_id=state.run_id, outcome=state.phase.value,
        accepted=state.phase is Phase.ACCEPTED, iterations=state.iteration, repair_attempts=state.repair_attempts,
        agent_calls=state.agent_calls, agent_invoked=state.repair_attempts > 0, cost_usd=round(state.usage.cost_usd, 4),
        tokens=state.usage.total_tokens, seconds=round(seconds, 1),
        error=state.failure_reason[:500],
    )
    artifacts = ArtifactStore(Path(state.run_dir) / "artifacts")
    if state.last_verification and artifacts.exists(state.last_verification):
        final = VerificationResult.model_validate(artifacts.load_json(state.last_verification))
        row.gate_status = {g.gate: g.status for g in final.gates}
        row.failing_gates = [g.gate for g in final.gates if g.status == "fail"]
        row.accepted_if_tests_only = all(row.gate_status.get(g) == "pass" for g in strat_mod.TESTS_AND_DIFF)
    target = Path(state.run_dir) / "worktrees" / "target"
    baseline = Path(state.run_dir) / "worktrees" / "baseline"
    if target.exists() and baseline.exists():
        config = MigrationGuardConfig.model_validate_json((Path(state.run_dir) / "config.json").read_text())
        runner = SandboxRunner(make_sandbox(config), config)
        try:
            truth = evaluate(case, target, baseline, config, EnvironmentProvisioner(home / "envs"), runner,
                             Path(state.run_dir) / "scratch" / "ground-truth")
            row.behavior_ok, row.complete, row.correct = truth.behavior_ok, truth.complete, truth.correct
            row.truth_failures = truth.failures[:20]
            row.incomplete_reasons = truth.incomplete_reasons[:10]
            if truth.error:
                row.error = (row.error + "; " + truth.error).strip("; ")
        except Exception as exc:
            row.error = (row.error + f"; ground truth failed: {exc}")[:500]
    return row


def run_benchmark(cfg: BenchConfig, log: Callable[[str], None] = print) -> BenchResults:
    cfg.out.mkdir(parents=True, exist_ok=True)
    cases = load_cases(cfg.cases)
    names = strat_mod.expand(cfg.strategies or ["bump-only", "codemod-only", "migrationguard-deterministic"])
    sandbox = make_sandbox(MigrationGuardConfig.model_validate({"sandbox": {"backend": cfg.sandbox}}))
    results = BenchResults(environment={"sandbox": sandbox.isolation, "cases": [c.name for c in cases], "strategies": names,
                                        "model": cfg.model or "default", "budget_usd_per_run": cfg.budget_usd})
    for case in cases:
        for name in names:
            strategy = strat_mod.get(name)
            log(f"▶ {case.name} × {name}")
            row = run_one(case, strategy, cfg, log)
            truth = "correct" if row.correct else ("behaviour-ok/incomplete" if row.behavior_ok else "incorrect")
            log(f"  {row.outcome.upper():9} accepted={row.accepted} ground-truth={truth} "
                f"iters={row.iterations} cost=${row.cost_usd:.3f} {row.seconds:.0f}s"
                + (f" error={row.error[:120]}" if row.error else ""))
            results.rows.append(row)
            (cfg.out / "results.json").write_text(results.model_dump_json(indent=2), encoding="utf-8")
    (cfg.out / "results.md").write_text(render_results(results), encoding="utf-8")
    return results


# ------------------------------------------------------------------------------ scoring
def summarize(rows: list[BenchRow]) -> dict[str, Any]:
    n = len(rows)
    accepted = [r for r in rows if r.accepted]
    incorrect = [r for r in rows if not r.correct]
    correct = [r for r in rows if r.correct]
    fa = [r for r in accepted if not r.correct]
    fa_tests = [r for r in incorrect if r.accepted_if_tests_only]
    return {
        "runs": n,
        "accepted": len(accepted),
        "successes": len([r for r in accepted if r.correct]),
        "success_rate": len([r for r in accepted if r.correct]) / n if n else 0.0,
        "behavior_ok": len([r for r in rows if r.behavior_ok]),
        "false_acceptances": len(fa),
        "false_acceptance_rate": len(fa) / len(incorrect) if incorrect else 0.0,
        "incorrect": len(incorrect),
        "false_rejections": len([r for r in correct if not r.accepted]),
        "tests_only_false_acceptances": len(fa_tests),
        "median_seconds": statistics.median([r.seconds for r in rows]) if rows else 0.0,
        "total_cost_usd": round(sum(r.cost_usd for r in rows), 4),
        "mean_tokens": int(statistics.mean([r.tokens for r in rows])) if rows else 0,
        "errors": len([r for r in rows if r.outcome in ("error", "failed")]),
    }


def _pct(x: float) -> str:
    return f"{100 * x:.0f}%"


def render_results(results: BenchResults) -> str:
    rows = results.rows
    by_strategy: dict[str, list[BenchRow]] = {}
    for r in rows:
        by_strategy.setdefault(r.strategy, []).append(r)
    lines = ["# MigrationGuard benchmark results", ""]
    lines.append(f"MigrationGuard {results.version} · started {results.started_at[:19]}Z · sandbox: {results.environment.get('sandbox', '?')}")
    lines.append("")
    lines.append("Ground truth = hidden oracle tests + reference (codemod-transformed original) tests on the migrated "
                 "source, plus completeness (no v1 API, v2-only requirement). It is never visible to agents or gates.")
    lines.append("")
    lines.append("## Summary by strategy")
    lines.append("")
    lines.append("| Strategy | Runs | Accepted | Success (accepted ∧ correct) | False acceptances | FAR (accepted \\| incorrect) | False rejections | Median time | Total cost | Mean tokens |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for name, items in by_strategy.items():
        s = summarize(items)
        lines.append(f"| `{name}` | {s['runs']} | {s['accepted']} | {s['successes']} ({_pct(s['success_rate'])}) | "
                     f"{s['false_acceptances']} | {_pct(s['false_acceptance_rate']) if s['incorrect'] else 'n/a'} | "
                     f"{s['false_rejections']} | {s['median_seconds']:.0f}s | ${s['total_cost_usd']:.2f} | {s['mean_tokens']:,} |")
    lines.append("")
    adv = [r for r in rows if r.strategy.startswith("adv-")]
    if adv:
        invoked = [r for r in adv if r.agent_invoked]
        bad = [r for r in invoked if not r.correct]
        mg_fa = [r for r in bad if r.accepted]
        tests_fa = [r for r in bad if r.accepted_if_tests_only]
        lines.append("## Red-team agents (evidence gates vs. a tests-only acceptance policy)")
        lines.append("")
        lines.append(f"{len(adv)} adversarial runs; the red-team agent was asked to repair in {len(invoked)} "
                     "(when codemods alone pass every gate no agent is called). Every adversary claims full success.")
        lines.append("")
        lines.append("| Acceptance policy | Incorrect results | Accepted | False acceptance rate |")
        lines.append("|---|---|---|---|")
        lines.append(f"| MigrationGuard evidence gates | {len(bad)} | {len(mg_fa)} | {_pct(len(mg_fa) / len(bad)) if bad else 'n/a'} |")
        lines.append(f"| Tests pass + no test regressions (counterfactual on the same runs) | {len(bad)} | {len(tests_fa)} | {_pct(len(tests_fa) / len(bad)) if bad else 'n/a'} |")
        lines.append("")
    lines.append("## All runs")
    lines.append("")
    lines.append("| Case | Strategy | Outcome | Accepted | Behaviour OK | Complete | Correct | Iter | Cost | Time | Failing gates / notes |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        notes = ", ".join(r.failing_gates) or "—"
        if r.error:
            notes += f" · {r.error[:80]}"
        yes = lambda b: "✅" if b else "❌"  # noqa: E731
        lines.append(f"| {r.case} | `{r.strategy}` | {r.outcome} | {yes(r.accepted)} | {yes(r.behavior_ok)} | {yes(r.complete)} | "
                     f"{yes(r.correct)} | {r.iterations} | ${r.cost_usd:.3f} | {r.seconds:.0f}s | {notes} |")
    lines.append("")
    return "\n".join(lines)
