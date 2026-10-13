"""The MigrationGuard orchestrator.

Drives a run through the explicit state machine (see :mod:`migrationguard.harness.state`):

    INIT → ANALYZE → BASELINE → PLAN → CODEMOD → VERIFY ⇄ REPAIR
                                                  │  ↖      ↑
                                                  ↓   REVIEW
                                                DECIDE → ACCEPTED | REJECTED
    (any phase) → PAUSED (budget / interrupt / --stop-after; resumable) | FAILED

Fault tolerance
---------------
* A checkpoint (full :class:`RunState` + target-worktree commit) is written after every
  transition. ``resume`` restores the worktree to the checkpoint commit and re-enters the
  phase, so a crash at any point loses at most the current phase's work.
* Every phase handler is idempotent and wrapped in a retry policy for transient failures
  (sandbox hiccups, dependency index outages, API rate limits); between retries the
  worktree is reset to the phase's starting commit.
* Budgets (USD, tokens, agent calls, wall time) are checked before every agent call;
  exhaustion pauses the run instead of failing it.
"""

from __future__ import annotations

import json
import os
import time
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from migrationguard.agents.base import AgentBackend, AgentTask, NullAgent
from migrationguard.agents.prompts import (
    REPAIR_SYSTEM_PROMPT,
    REVIEWER_SYSTEM_PROMPT,
    build_repair_prompt,
    build_review_prompt,
)
from migrationguard.agents.tools import AgentToolbox, ToolRuntime
from migrationguard.artifacts import ArtifactStore
from migrationguard.config import MigrationGuardConfig, home_dir
from migrationguard.errors import (
    AgentQuotaExceeded,
    AgentUnavailable,
    BudgetExceeded,
    EnvironmentProvisionError,
    MigrationGuardError,
    RunInterrupted,
    TransientError,
)
from migrationguard.harness.events import EventLog, EventSink
from migrationguard.harness.state import BudgetTracker, RunState, VerificationRecord, with_retries
from migrationguard.harness.store import Store
from migrationguard.migrate.deps import collect_requirements
from migrationguard.migrate.recipe import PydanticV2Recipe
from migrationguard.models import Decision, GateResult, Phase, VerificationResult
from migrationguard.sandbox.envs import EnvironmentProvisioner, EnvSpec, ProvisionedEnv
from migrationguard.sandbox.git import GitRepo, Worktree, WorktreeManager
from migrationguard.sandbox.policy import PermissionPolicy
from migrationguard.sandbox.runner import SandboxRunner, make_sandbox
from migrationguard.verify.compare import diff_probes
from migrationguard.verify.gates import BaselineEvidence, VerificationContext, Verifier

AgentFactory = Callable[[MigrationGuardConfig, EventLog], AgentBackend]
FaultInjector = Callable[[Phase, RunState], None]


@dataclass
class RunOptions:
    agent: str | None = None
    sandbox: str | None = None
    model: str | None = None
    max_repair_iterations: int | None = None
    budget_usd: float | None = None
    reviewer: bool | None = None
    probes: bool | None = None
    stop_after: Phase | None = None
    include_uncommitted: bool = False
    run_id: str | None = None
    #: True = all codemods; "tests" = only the audited codemod on protected test files (the agent
    #: migrates the source); False = only bump the dependency (the "naive upgrade" baseline)
    codemods: bool | str = True
    #: arbitrary config overrides (merged last), e.g. {"verify": {"only_gates": [...]}}
    extra_overrides: dict[str, Any] | None = None

    def overrides(self) -> dict[str, Any]:
        from migrationguard.config import deep_merge

        return deep_merge(self._base_overrides(), self.extra_overrides or {})

    def _base_overrides(self) -> dict[str, Any]:
        return {
            "agent": {"backend": self.agent, "model": self.model, "max_repair_iterations": self.max_repair_iterations},
            "sandbox": {"backend": self.sandbox},
            "budget": {"max_cost_usd": self.budget_usd},
            "verify": {"reviewer": self.reviewer, "probes": self.probes},
        }


@dataclass
class RunContext:
    state: RunState
    config: MigrationGuardConfig
    store: Store
    events: EventLog
    artifacts: ArtifactStore
    repo: GitRepo
    manager: WorktreeManager
    recipe: PydanticV2Recipe
    provisioner: EnvironmentProvisioner
    runner: SandboxRunner
    agent: AgentBackend
    budget: BudgetTracker
    scratch: Path
    target: Worktree | None = None
    baseline: Worktree | None = None
    _envs: dict[str, ProvisionedEnv] = field(default_factory=dict)

    @property
    def run_dir(self) -> Path:
        return Path(self.state.run_dir)


class Orchestrator:
    def __init__(
        self,
        home: Path | None = None,
        sink: EventSink | None = None,
        agent_factory: AgentFactory | None = None,
        fault_injector: FaultInjector | None = None,
    ):
        self.home = home or home_dir()
        self.store = Store(self.home / "migrationguard.db")
        self.sink = sink
        self.agent_factory = agent_factory
        self.fault_injector = fault_injector or env_fault_injector()

    # ================================================================== public API
    def start(self, repo_path: Path, options: RunOptions | None = None) -> RunState:
        options = options or RunOptions()
        repo = GitRepo.open(repo_path)
        config = MigrationGuardConfig.load(repo.root, options.overrides())
        base = repo.snapshot(options.include_uncommitted)
        run_id = options.run_id or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        run_dir = self.home / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "config.json").write_text(config.model_dump_json(indent=2), encoding="utf-8")
        state = RunState(
            run_id=run_id,
            repo=str(repo.root),
            run_dir=str(run_dir),
            branch=f"migrationguard/{run_id}",
            base_commit=base,
            stop_after=options.stop_after,
            options={"include_uncommitted": options.include_uncommitted, "codemods": options.codemods},
        )
        self.store.create_run(run_id, state.repo, state.branch, base, str(run_dir), Phase.INIT.value)
        ctx = self._context(state, config)
        ctx.events.emit("run.start", f"run {run_id} on {repo.root} (base {base[:12]})", base=base)
        self._checkpoint(ctx)
        return self._drive(ctx)

    def resume(self, run_id: str, options: RunOptions | None = None) -> RunState:
        row = self.store.get_run(run_id)
        if row is None:
            raise MigrationGuardError(f"unknown run {run_id}")
        checkpoint = self.store.latest_checkpoint(row["id"])
        if checkpoint is None:
            raise MigrationGuardError(f"run {run_id} has no checkpoint")
        state = RunState.model_validate_json(checkpoint["state_json"])
        if state.phase.terminal:
            return state
        config = MigrationGuardConfig.model_validate_json((Path(state.run_dir) / "config.json").read_text())
        if options:
            from migrationguard.config import deep_merge

            config = MigrationGuardConfig.model_validate(deep_merge(config.model_dump(), options.overrides()))
            (Path(state.run_dir) / "config.json").write_text(config.model_dump_json(indent=2), encoding="utf-8")
            if options.stop_after is not None:
                state.stop_after = options.stop_after
        interrupted = self.store.mark_interrupted_attempts(state.run_id)
        if state.phase is Phase.PAUSED:
            state.phase = state.paused_from or Phase.INIT
            state.paused_from = None
            state.pause_reason = ""
        ctx = self._context(state, config)
        ctx.events.emit(
            "run.resume",
            f"resuming run {state.run_id} at phase {state.phase.value}"
            + (f" ({interrupted} interrupted agent attempt(s) discarded)" if interrupted else ""),
            commit=checkpoint["commit_sha"],
        )
        if state.phase is not Phase.INIT:
            ctx.target, ctx.baseline = ctx.manager.open()
            if checkpoint["commit_sha"]:
                ctx.target.reset_hard(checkpoint["commit_sha"])
        return self._drive(ctx)

    # ================================================================== plumbing
    def _context(self, state: RunState, config: MigrationGuardConfig) -> RunContext:
        run_dir = Path(state.run_dir)
        events = EventLog(self.store, state.run_id, run_dir / "events.jsonl", self.sink)
        repo = GitRepo(Path(state.repo))
        sandbox = make_sandbox(config, log=events.warn)
        state.sandbox_backend = sandbox.name
        state.sandbox_isolation = sandbox.isolation
        agent = self._make_agent(config, events)
        state.agent_backend = agent.name
        return RunContext(
            state=state,
            config=config,
            store=self.store,
            events=events,
            artifacts=ArtifactStore(run_dir / "artifacts"),
            repo=repo,
            manager=WorktreeManager(repo, run_dir),
            recipe=PydanticV2Recipe(config.migration.target_requirement, config.migration.settings_requirement),
            provisioner=EnvironmentProvisioner(self.home / "envs"),
            runner=SandboxRunner(sandbox, config),
            agent=agent,
            budget=BudgetTracker(config.budget, state),
            scratch=run_dir / "scratch",
        )

    def _make_agent(self, config: MigrationGuardConfig, events: EventLog) -> AgentBackend:
        if self.agent_factory is not None:
            return self.agent_factory(config, events)
        if config.agent.backend == "none":
            return NullAgent()
        try:
            from migrationguard.agents.claude_sdk import ClaudeAgentBackend

            return ClaudeAgentBackend(config.agent.model, config.agent.reviewer_model,
                                      log=lambda kind, data: events.emit(kind, data.get("text") or data.get("tool", ""), "debug", **data))
        except AgentUnavailable as exc:
            events.warn(f"agent backend unavailable ({exc}); continuing with deterministic migration only")
            return NullAgent()

    def _checkpoint(self, ctx: RunContext) -> None:
        commit = None
        if ctx.target is not None and (ctx.target.path / ".git").exists():
            commit = ctx.target.head()
        self.store.checkpoint(ctx.state.run_id, ctx.state.phase.value, commit, ctx.state.model_dump_json())

    def _advance(self, ctx: RunContext, target: Phase) -> None:
        previous = ctx.state.phase
        ctx.state.transition(target)
        ctx.events.emit("phase.transition", f"{previous.value} -> {target.value}", src=previous.value, dst=target.value)
        self._checkpoint(ctx)

    def _pause(self, ctx: RunContext, reason: str) -> None:
        ctx.state.pause_reason = reason
        self._advance(ctx, Phase.PAUSED)
        ctx.events.emit("run.paused", f"paused: {reason} (resume with `migrationguard resume {ctx.state.run_id}`)", "warning")

    def _drive(self, ctx: RunContext) -> RunState:
        state = ctx.state
        while not state.phase.terminal and state.phase is not Phase.PAUSED:
            phase = state.phase
            ctx.events.phase = phase.value
            ctx.events.emit("phase.start", f"phase {phase.value}")
            started = time.monotonic()
            start_commit = ctx.target.head() if ctx.target is not None and phase is not Phase.INIT else None
            try:

                def on_retry(attempt: int, exc: Exception, delay: float, _phase=phase, _commit=start_commit) -> None:
                    state.phase_retries[_phase.value] = state.phase_retries.get(_phase.value, 0) + 1
                    ctx.events.warn(f"transient failure in {_phase.value} (attempt {attempt}): {exc}; retrying in {delay:.1f}s")
                    if _commit and ctx.target is not None:
                        ctx.target.reset_hard(_commit)

                def attempt(_phase=phase) -> Phase:
                    if self.fault_injector:
                        self.fault_injector(_phase, state)
                    return HANDLERS[_phase](ctx)

                next_phase = with_retries(attempt, ctx.config.retry, on_retry=on_retry)
            except (BudgetExceeded, AgentQuotaExceeded) as exc:
                state.active_seconds += time.monotonic() - started
                if start_commit and ctx.target is not None:
                    ctx.target.reset_hard(start_commit)
                self._pause(ctx, str(exc))
                break
            except TransientError as exc:
                # still failing after the retry policy: resumable, so pause instead of failing
                state.active_seconds += time.monotonic() - started
                if start_commit and ctx.target is not None:
                    ctx.target.reset_hard(start_commit)
                self._pause(ctx, f"transient failure persisted in {phase.value} after retries: {exc}")
                break
            except (RunInterrupted, KeyboardInterrupt) as exc:
                state.active_seconds += time.monotonic() - started
                if start_commit and ctx.target is not None:
                    ctx.target.reset_hard(start_commit)
                self._pause(ctx, f"interrupted during {phase.value}: {exc or 'keyboard interrupt'}")
                break
            except MigrationGuardError as exc:
                state.active_seconds += time.monotonic() - started
                self._fail(ctx, f"{phase.value}: {exc}", traceback.format_exc())
                break
            except Exception as exc:  # unexpected bug: fail loudly, keep evidence
                state.active_seconds += time.monotonic() - started
                self._fail(ctx, f"{phase.value}: unexpected {type(exc).__name__}: {exc}", traceback.format_exc())
                break
            state.active_seconds += time.monotonic() - started
            ctx.events.emit("phase.end", f"phase {phase.value} done in {time.monotonic() - started:.1f}s",
                            seconds=round(time.monotonic() - started, 2))
            self._advance(ctx, next_phase)
            if state.stop_after is phase and not state.phase.terminal:
                state.stop_after = None
                self._pause(ctx, f"stopped after {phase.value} as requested")
                break
        if state.phase.terminal:
            ctx.events.emit("run.end", f"run finished: {state.phase.value.upper()}"
                            + (f" — {state.decision.reason}" if state.decision else f" — {state.failure_reason}"))
        self.store.update_run(state.run_id, state.phase.value, state.decision.model_dump() if state.decision else None)
        return state

    def _fail(self, ctx: RunContext, reason: str, trace: str) -> None:
        ctx.state.failure_reason = reason
        ctx.events.emit("run.failed", reason, "error", traceback=trace[-4000:])
        ctx.state.decision = Decision(outcome="failed", reason=reason, branch=ctx.state.branch)
        self._advance(ctx, Phase.FAILED)
        try:
            from migrationguard.report.render import write_reports

            write_reports(ctx.state, self.store, ctx.artifacts)
        except Exception:  # reporting must never mask the original failure
            pass


def env_fault_injector() -> FaultInjector | None:
    """Fault injection for resilience testing, driven by ``MIGRATIONGUARD_FAULT``.

    Format: ``<phase>:<action>[:<times>]`` (comma separated for several), e.g.
    ``verify:crash`` (hard process exit after the phase's checkpoint), ``repair:interrupt``
    or ``baseline:transient:2``. Each fault fires ``times`` times (default 1) per process.
    """
    spec = os.environ.get("MIGRATIONGUARD_FAULT", "").strip()
    if not spec:
        return None
    faults: dict[str, list] = {}
    for item in spec.split(","):
        parts = item.strip().split(":")
        if len(parts) < 2:
            continue
        faults[parts[0]] = [parts[1], int(parts[2]) if len(parts) > 2 else 1]

    def inject(phase: Phase, state: RunState) -> None:
        fault = faults.get(phase.value)
        if not fault or fault[1] <= 0:
            return
        fault[1] -= 1
        action = fault[0]
        if action == "crash":
            os._exit(137)
        if action == "interrupt":
            raise RunInterrupted(f"injected interrupt in {phase.value}")
        if action == "transient":
            raise TransientError(f"injected transient failure in {phase.value}")
        if action == "error":
            raise MigrationGuardError(f"injected failure in {phase.value}")

    return inject


# ====================================================================== phase handlers
def _env(ctx: RunContext, worktree: Worktree, label: str, constraints: list[str]) -> ProvisionedEnv:
    reqs = collect_requirements(worktree.path, ctx.config.environment.requirements or None)
    reqs += ctx.config.environment.extra_requirements
    key = label + "|" + "|".join(sorted(reqs)) + "|" + "|".join(constraints)
    if key in ctx._envs:
        return ctx._envs[key]
    spec = EnvSpec(label=label, python=ctx.config.environment.python, requirements=reqs, constraints=constraints,
                   exclude_newer=ctx.config.environment.exclude_newer)
    env = ctx.provisioner.provision(spec)
    ctx.events.info(f"{label} environment ready (lock {env.lock_hash}, pydantic {env.version_of('pydantic')}, "
                    f"{'cached' if env.cached else 'installed'})", packages=env.packages)
    ctx._envs[key] = env
    return env


def phase_init(ctx: RunContext) -> Phase:
    ctx.target, ctx.baseline = ctx.manager.create(ctx.state.branch, ctx.state.base_commit)
    ctx.events.info(f"worktrees: target={ctx.target.path} baseline={ctx.baseline.path}; branch {ctx.state.branch}")
    ctx.events.info(f"sandbox: {ctx.state.sandbox_isolation}")
    ctx.events.info(f"agent backend: {ctx.state.agent_backend}")
    return Phase.ANALYZE


def phase_analyze(ctx: RunContext) -> Phase:
    assert ctx.baseline is not None
    analysis = ctx.recipe.analyze(ctx.baseline.path)
    summary = analysis.summary()
    ctx.state.analysis_summary = summary
    ctx.artifacts.save_json("analysis/findings.json", [f.model_dump() for f in analysis.findings])
    ctx.artifacts.save_json("analysis/summary.json", summary)
    ctx.events.emit("analysis", f"{summary['models']} model(s), {summary['findings']} finding(s): "
                    f"{summary['auto_fixable']} auto-fixable, {summary['unresolved']} need an agent", summary=summary)
    if not analysis.index.uses_pydantic() and not analysis.pydantic_specs:
        raise MigrationGuardError("the project does not use pydantic; nothing to migrate")
    if analysis.pydantic_specs and not analysis.auto_fixable and not analysis.unresolved:
        raise MigrationGuardError("no Pydantic v1 usage found (already migrated?)")
    return Phase.BASELINE


def phase_baseline(ctx: RunContext) -> Phase:
    assert ctx.baseline is not None
    env = _env(ctx, ctx.baseline, "baseline", ["pydantic<2"])
    out = ctx.scratch / "baseline"
    tests = ctx.runner.run_tests(env, ctx.baseline.path, out, "baseline")
    counts = tests.counts()
    ctx.events.emit("baseline.tests", f"baseline tests: {counts} (pydantic {tests.packages.get('pydantic', '?')})", counts=counts)
    if not tests.tests and tests.collection_errors:
        raise MigrationGuardError("baseline test suite cannot be collected: " + tests.collection_errors[0][-800:])
    if counts.get("passed", 0) == 0:
        raise MigrationGuardError("no baseline test passes; there is no behavioural oracle to migrate against")
    evidence = BaselineEvidence(test_run=tests, packages=env.packages, lock_hash=env.lock_hash)
    if ctx.config.verify.probes:
        inputs = "baseline/probe-inputs.json"
        try:
            probes = ctx.runner.run_probes(env, ctx.baseline.path, out, "baseline", ctx.artifacts.path(inputs), discover=True)
            evidence.probe_run = probes
            evidence.probe_inputs = inputs
            ctx.events.emit("baseline.probes", f"{len(probes.outcomes)} behavioural probe(s) recorded on pydantic {probes.pydantic_version}"
                            + (f"; {len(probes.import_errors)} import error(s)" if probes.import_errors else ""))
        except MigrationGuardError as exc:
            ctx.events.warn(f"behavioural probes unavailable for this project: {exc}")
    ctx.artifacts.save_text("baseline/lock.txt", env.lock_text)
    ctx.artifacts.save_json("baseline/evidence.json", evidence)
    ctx.state.baseline_artifact = "baseline/evidence.json"
    return Phase.PLAN


def phase_plan(ctx: RunContext) -> Phase:
    assert ctx.baseline is not None
    analysis = ctx.recipe.analyze(ctx.baseline.path)
    plan = ctx.recipe.plan(analysis)
    ctx.state.plan = plan
    ctx.artifacts.save_json("plan.json", plan)
    for step in plan.steps:
        ctx.events.emit("plan.step", f"[{step.kind}] {step.title}" + (f" ({step.detail})" if step.kind != "agent" else ""), step=step.id)
    return Phase.CODEMOD


def phase_codemod(ctx: RunContext) -> Phase:
    assert ctx.target is not None
    ctx.target.reset_hard(ctx.state.base_commit)
    mode = ctx.state.options.get("codemods", True)
    result = ctx.recipe.apply(ctx.target.path, code=mode is not False, tests_only=mode == "tests")
    changes = sum(len(e.changes) for e in result.file_edits)
    sha = ctx.target.commit_all(f"migrationguard: deterministic Pydantic v2 codemods ({changes} changes)")
    ctx.state.commits["codemod"] = sha
    ctx.state.codemod_summary = {
        "files": len(result.file_edits),
        "changes": changes,
        "dependency_files": [e.path for e in result.dependency_edits],
        "unresolved": len(result.unresolved),
        "needs_settings_dependency": result.needs_settings_dependency,
    }
    ctx.artifacts.save_json("codemod.json", {
        "file_edits": [e.model_dump() for e in result.file_edits],
        "dependency_edits": [{"path": e.path, "description": e.description} for e in result.dependency_edits],
        "unresolved": [f.model_dump() for f in result.unresolved],
    })
    ctx.events.emit("codemod", f"applied {changes} change(s) in {len(result.file_edits)} file(s); "
                    f"{len(result.unresolved)} unresolved; commit {sha[:12]}", **ctx.state.codemod_summary)
    return Phase.VERIFY


def _baseline_evidence(ctx: RunContext) -> BaselineEvidence:
    return BaselineEvidence.model_validate(ctx.artifacts.load_json(ctx.state.baseline_artifact or "baseline/evidence.json"))


def _verifier(ctx: RunContext) -> Verifier:
    assert ctx.target is not None and ctx.baseline is not None
    return Verifier(VerificationContext(
        config=ctx.config, runner=ctx.runner, recipe=ctx.recipe, target=ctx.target, baseline=ctx.baseline,
        base_commit=ctx.state.base_commit, artifacts=ctx.artifacts, scratch=ctx.scratch, log=ctx.events.info,
    ))


def _target_env(ctx: RunContext) -> tuple[ProvisionedEnv | None, str | None]:
    assert ctx.target is not None
    try:
        return with_retries(lambda: _env(ctx, ctx.target, "target", []), ctx.config.retry,
                            on_retry=lambda a, e, d: ctx.events.warn(f"provisioning retry {a}: {str(e)[:200]}")), None
    except EnvironmentProvisionError as exc:
        return None, str(exc)


def phase_verify(ctx: RunContext) -> Phase:
    assert ctx.target is not None
    state = ctx.state
    state.iteration += 1
    baseline = _baseline_evidence(ctx)
    env, env_error = _target_env(ctx)
    output = _verifier(ctx).verify(state.iteration, baseline, env, env_error)
    result = output.result
    ctx.store.add_evidence(state.run_id, state.iteration, result.commit, result.gates)
    name = f"iter-{state.iteration:02d}/verification.json"
    ctx.artifacts.save_json(name, result)
    state.last_verification = name
    trigger = "codemod" if state.repair_attempts == 0 else f"repair-{state.repair_attempts}"
    state.history.append(VerificationRecord(iteration=state.iteration, commit=result.commit, passed=result.passed,
                                            failing_gates=[g.gate for g in result.failing_gates()], trigger=trigger))
    for gate in result.gates:
        level = {"pass": "info", "warn": "warning", "skip": "info"}.get(gate.status, "error" if gate.blocking else "warning")
        ctx.events.emit("gate", f"{gate.gate}: {gate.status.upper()} — {gate.summary}", level, gate=gate.gate, status=gate.status)
    agent_available = ctx.agent.name != "none"
    if result.passed:
        if agent_available and ctx.config.verify.reviewer:
            return Phase.REVIEW
        return Phase.DECIDE
    if not agent_available:
        ctx.events.info("verification failed and no agent is configured; deciding on the evidence")
        return Phase.DECIDE
    if state.repair_attempts >= ctx.config.agent.max_repair_iterations:
        ctx.events.info(f"verification failed after {state.repair_attempts} repair attempt(s); repair budget exhausted")
        return Phase.DECIDE
    return Phase.REPAIR


def _load_verification(ctx: RunContext) -> VerificationResult:
    return VerificationResult.model_validate(ctx.artifacts.load_json(ctx.state.last_verification or ""))


def _runtime(ctx: RunContext, verification: VerificationResult) -> ToolRuntime:
    assert ctx.target is not None
    baseline = _baseline_evidence(ctx)

    def run_tests(node_ids: list[str]) -> str:
        env, err = _target_env(ctx)
        if env is None:
            return f"cannot provision the target environment: {err}"
        run = ctx.runner.run_tests(env, ctx.target.path, ctx.scratch / "agent", "agent", extra_args=node_ids)
        lines = [f"exit code {run.exit_code}; counts {run.counts()}"]
        lines += [f"COLLECTION ERROR: {e[-1500:]}" for e in run.collection_errors[:3]]
        for t in run.failing[:15]:
            lines.append(f"{t.outcome.upper()} {t.nodeid}\n" + "\n".join(t.message.splitlines()[-25:]))
        pre = {n for n, t in baseline.test_run.tests.items() if t.outcome in ("failed", "error")}
        if pre:
            lines.append(f"(pre-existing baseline failures, may stay failing: {sorted(pre)[:10]})")
        return "\n".join(lines)[:12000]

    def run_probes() -> str:
        if baseline.probe_run is None or not baseline.probe_inputs:
            return "behavioural probes are not available for this project"
        env, err = _target_env(ctx)
        if env is None:
            return f"cannot provision the target environment: {err}"
        try:
            target = ctx.runner.run_probes(env, ctx.target.path, ctx.scratch / "agent", "agent",
                                           ctx.artifacts.path(baseline.probe_inputs), discover=False)
        except MigrationGuardError as exc:
            return f"probe runner failed: {exc}"
        diffs = diff_probes(baseline.probe_run, target, ctx.config.verify.accepted_behavior_changes,
                            ctx.config.verify.error_location_diff_blocking)
        blocking = [d for d in diffs if d.blocking]
        lines = [f"{len(baseline.probe_run.outcomes)} probes; {len(blocking)} blocking difference(s)"]
        lines += [f"import error: {e}" for e in target.import_errors[:5]]
        for d in blocking[:20]:
            lines.append(f"- {d.probe_id} [{d.kind}]\n  baseline: {json.dumps(d.baseline, default=str)[:400]}\n"
                         f"  target:   {json.dumps(d.target, default=str)[:400]}")
        return "\n".join(lines)[:12000]

    def findings() -> str:
        residual = ctx.recipe.residual(ctx.target.path, ctx.config.verify.allowed_residual_rules)
        if not residual:
            return "no residual Pydantic v1 usage found"
        return "\n".join(f"{f.path}:{f.line} {f.rule} {f.message}" + (f"  `{f.snippet}`" if f.snippet else "") for f in residual[:80])

    def diff() -> str:
        text = ctx.target.diff(ctx.state.base_commit)
        work = ctx.target.working_diff()
        out = text + ("\n# --- uncommitted changes in this attempt ---\n" + work if work.strip() else "")
        return out[:40000] + ("\n... diff truncated" if len(out) > 40000 else "")

    def evidence() -> str:
        return json.dumps([g.model_dump() for g in verification.gates], indent=1, default=str)[:30000]

    return ToolRuntime(run_tests=run_tests, run_probes=run_probes, findings=findings, diff=diff, evidence=evidence)


def _toolbox(ctx: RunContext, verification: VerificationResult, read_only: bool) -> AgentToolbox:
    assert ctx.target is not None
    policy = PermissionPolicy(ctx.target.path, ctx.config.policy)
    return AgentToolbox(
        root=ctx.target.path,
        policy=policy,
        runtime=_runtime(ctx, verification),
        max_test_runs=ctx.config.agent.max_tool_test_runs,
        log=lambda name, data: ctx.events.emit("agent.tool", f"{name} {data.get('denied') or data.get('error') or ''}".strip(),
                                               "warning" if data.get("denied") else "debug", tool=name, **data),
        read_only=read_only,
    )


def phase_repair(ctx: RunContext) -> Phase:
    assert ctx.target is not None
    state = ctx.state
    ctx.budget.check()
    verification = _load_verification(ctx)
    residual = ctx.recipe.residual(ctx.target.path, ctx.config.verify.allowed_residual_rules)
    history = [{"iteration": a["iteration"], "summary": a.get("summary") or "",
                "failing": next((h.failing_gates for h in state.history if h.iteration == a["iteration"] + 1), [])}
               for a in ctx.store.attempts(state.run_id) if a["role"] == "repair" and a["status"] == "finished"]
    attempt_no = state.repair_attempts + 1
    prompt = build_repair_prompt(state.plan, verification, residual, history,
                                 state.review if state.review_feedback_pending else None,
                                 ctx.config.agent.prompt_evidence_chars, attempt_no)
    ctx.artifacts.save_text(f"agent/repair-{attempt_no:02d}-prompt.md", prompt)
    toolbox = _toolbox(ctx, verification, read_only=False)
    task = AgentTask(role="repair", system_prompt=REPAIR_SYSTEM_PROMPT, prompt=prompt, toolbox=toolbox,
                     worktree=ctx.target.path, max_turns=ctx.config.agent.max_turns,
                     max_budget_usd=ctx.budget.remaining_usd(), model=ctx.config.agent.model,
                     iteration=attempt_no, base_commit=state.base_commit)
    before = ctx.target.head()
    attempt_id = ctx.store.start_attempt(state.run_id, attempt_no, "repair")
    ctx.events.emit("agent.start", f"repair attempt {attempt_no} with {ctx.agent.name}")
    try:
        result = ctx.agent.repair(task)
    except Exception as exc:
        ctx.store.finish_attempt(attempt_id, None, "error", None, error=str(exc)[:1000])
        raise
    result.tool_calls = max(result.tool_calls, toolbox.calls)
    result.denied_tool_calls = max(result.denied_tool_calls, toolbox.denied)
    ctx.budget.record(result.usage)
    ctx.store.add_usage(state.run_id, Phase.REPAIR.value, "repair", ctx.config.agent.model, result.usage)
    sha = ctx.target.commit_all(f"migrationguard: repair attempt {attempt_no} ({ctx.agent.name})")
    ctx.store.finish_attempt(attempt_id, result, result.status, sha, error=result.error)
    ctx.artifacts.save_json(f"agent/repair-{attempt_no:02d}-result.json", result)
    state.repair_attempts = attempt_no
    state.review_feedback_pending = False
    state.commits[f"repair-{attempt_no}"] = sha
    ctx.events.emit(
        "agent.end",
        f"repair attempt {attempt_no}: {result.status}; {result.tool_calls} tool call(s), {result.denied_tool_calls} denied; "
        f"${result.usage.cost_usd:.4f}, {result.usage.total_tokens} tokens; claims: {[c.text for c in result.claims][:5]}",
        status=result.status, cost=result.usage.cost_usd, tokens=result.usage.total_tokens,
    )
    if sha == before:
        ctx.events.warn("the agent made no changes; verification would be identical")
        return Phase.DECIDE
    return Phase.VERIFY


def phase_review(ctx: RunContext) -> Phase:
    assert ctx.target is not None
    state = ctx.state
    ctx.budget.check()
    verification = _load_verification(ctx)
    prompt = build_review_prompt(ctx.target.diff(state.base_commit, stat=True)[-6000:], verification,
                                 ctx.config.agent.prompt_evidence_chars)
    toolbox = _toolbox(ctx, verification, read_only=True)
    task = AgentTask(role="reviewer", system_prompt=REVIEWER_SYSTEM_PROMPT, prompt=prompt, toolbox=toolbox,
                     worktree=ctx.target.path, max_turns=min(30, ctx.config.agent.max_turns),
                     max_budget_usd=ctx.budget.remaining_usd(), model=ctx.config.agent.reviewer_model,
                     iteration=state.iteration, base_commit=state.base_commit)
    attempt_id = ctx.store.start_attempt(state.run_id, state.iteration, "reviewer")
    before = ctx.target.head()
    result = ctx.agent.review(task)
    ctx.budget.record(result.usage)
    ctx.store.add_usage(state.run_id, Phase.REVIEW.value, "reviewer", ctx.config.agent.reviewer_model, result.usage)
    ctx.store.finish_attempt(attempt_id, result, result.status, ctx.target.head(), error=result.error)
    if not ctx.target.is_clean() or ctx.target.head() != before:
        # the reviewer is read-only: any change is a policy breach and is discarded
        ctx.events.error("reviewer modified the worktree; changes discarded")
        ctx.target.reset_hard(before)
    verdict = result.review
    if verdict is None:
        gate = GateResult(gate="review.independent", title="Independent reviewer agent", status="warn", blocking=False,
                          summary=f"reviewer produced no verdict ({result.status}: {result.error[:200]})")
    elif verdict.verdict == "approve":
        gate = GateResult(gate="review.independent", title="Independent reviewer agent", status="pass", blocking=False,
                          summary=verdict.summary[:300], details={"issues": [i.model_dump() for i in verdict.issues]})
    else:
        gate = GateResult(gate="review.independent", title="Independent reviewer agent", status="fail", blocking=True,
                          summary=verdict.summary[:300], details={"issues": [i.model_dump() for i in verdict.issues]})
    state.review = verdict
    verification.gates = [g for g in verification.gates if g.gate != "review.independent"] + [gate]
    ctx.artifacts.save_json(state.last_verification or "", verification)
    ctx.store.add_evidence(state.run_id, state.iteration, verification.commit, [gate])
    ctx.artifacts.save_json(f"agent/review-{state.iteration:02d}.json", result)
    ctx.events.emit("gate", f"review.independent: {gate.status.upper()} — {gate.summary}",
                    "info" if gate.status != "fail" else "error", gate=gate.gate, status=gate.status)
    if state.history:
        state.history[-1].passed = verification.passed
        state.history[-1].failing_gates = [g.gate for g in verification.failing_gates()]
    if gate.status == "fail" and state.repair_attempts < ctx.config.agent.max_repair_iterations:
        state.review_feedback_pending = True
        return Phase.REPAIR
    return Phase.DECIDE


def phase_decide(ctx: RunContext) -> Phase:
    assert ctx.target is not None
    state = ctx.state
    verification = _load_verification(ctx)
    patch = ctx.target.diff(state.base_commit)
    ctx.artifacts.save_text("migration.patch", patch)
    failing = [g.gate for g in verification.failing_gates()]
    if verification.passed:
        reason = f"all blocking gates passed at iteration {verification.iteration}"
        outcome = "accepted"
    else:
        reason = f"blocking gates failed: {', '.join(failing)}"
        outcome = "rejected"
    state.decision = Decision(outcome=outcome, reason=reason, blocking_gates=failing,  # type: ignore[arg-type]
                              final_commit=verification.commit, branch=state.branch)
    from migrationguard.report.render import write_reports

    paths = write_reports(state, ctx.store, ctx.artifacts)
    ctx.events.emit("decision", f"{outcome.upper()}: {reason}", "info" if outcome == "accepted" else "warning",
                    report=str(paths.get("markdown")))
    return Phase.ACCEPTED if outcome == "accepted" else Phase.REJECTED


HANDLERS: dict[Phase, Callable[[RunContext], Phase]] = {
    Phase.INIT: phase_init,
    Phase.ANALYZE: phase_analyze,
    Phase.BASELINE: phase_baseline,
    Phase.PLAN: phase_plan,
    Phase.CODEMOD: phase_codemod,
    Phase.VERIFY: phase_verify,
    Phase.REPAIR: phase_repair,
    Phase.REVIEW: phase_review,
    Phase.DECIDE: phase_decide,
}

