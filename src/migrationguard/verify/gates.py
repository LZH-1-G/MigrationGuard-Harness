"""Evidence gates.

Each gate turns a deterministic observation into a :class:`GateResult` with a status,
a blocking flag and the artifacts (by SHA-256) it is based on. A migration is accepted
only if no blocking gate fails. Nothing an agent *says* is an input to any gate here; the
independent reviewer is a separate, veto-only gate added by the review phase.

Gates
-----
static.syntax        every Python file compiles
static.residual      dry-run codemod over the migrated tree finds no v1 API left
deps.target          manifests require pydantic v2 AND the resolved/imported version is 2.x
tests.pass           migrated test suite is green (pre-existing failures excepted)
tests.differential   every test that passed on the baseline passes after migration
tests.integrity      test changes are re-derivable from the codemod; no new skip/xfail,
                     no deleted tests, no start-up hooks, no pytest config edits, no
                     test-detecting production code
tests.frozen         the ORIGINAL tests, run against the migrated code (advisory)
runtime.deprecations no Pydantic deprecation warnings attributable to project code
behavior.probes      differential behavioural probes agree (or changes were accepted by
                     a human in migrationguard.toml)
scope.diff           only writable paths changed; no source files deleted
"""

from __future__ import annotations

import re
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field

from migrationguard.analysis.project import ProjectIndex, is_test_path, iter_python_files, matches_any
from migrationguard.artifacts import ArtifactStore
from migrationguard.config import MigrationGuardConfig
from migrationguard.migrate.deps import (
    declared_pydantic_specs,
    pydantic_requirements,
    spec_allows_v1,
    spec_allows_v2,
)
from migrationguard.migrate.recipe import PydanticV2Recipe
from migrationguard.models import GateResult, ProbeRunResult, TestRunResult, VerificationResult
from migrationguard.sandbox.envs import ProvisionedEnv
from migrationguard.sandbox.git import Worktree
from migrationguard.sandbox.policy import STARTUP_HOOK_NAMES
from migrationguard.sandbox.runner import SandboxRunner
from migrationguard.verify.compare import (
    assertion_count,
    diff_probes,
    diff_tests,
    new_skip_markers,
    project_deprecations,
    suspicious_additions,
)

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

MAX_LIST = 40


class BaselineEvidence(BaseModel):
    test_run: TestRunResult
    probe_run: ProbeRunResult | None = None
    probe_inputs: str | None = None  # artifact name
    packages: dict[str, str] = Field(default_factory=dict)
    lock_hash: str = ""


@dataclass
class VerificationContext:
    config: MigrationGuardConfig
    runner: SandboxRunner
    recipe: PydanticV2Recipe
    target: Worktree
    baseline: Worktree
    base_commit: str
    artifacts: ArtifactStore
    scratch: Path
    log: Callable[[str], None] = lambda msg: None


@dataclass
class VerificationOutput:
    result: VerificationResult
    target_tests: TestRunResult | None = None
    probe_run: ProbeRunResult | None = None
    residual: list = field(default_factory=list)


class Verifier:
    def __init__(self, ctx: VerificationContext):
        self.ctx = ctx
        self._base_index: ProjectIndex | None = None

    @property
    def base_index(self) -> ProjectIndex:
        if self._base_index is None:
            self._base_index = ProjectIndex.build(self.ctx.baseline.path)
        return self._base_index

    # ------------------------------------------------------------------ entry point
    def verify(
        self,
        iteration: int,
        baseline: BaselineEvidence,
        target_env: ProvisionedEnv | None,
        env_error: str | None = None,
    ) -> VerificationOutput:
        ctx = self.ctx
        commit = ctx.target.head()
        prefix = f"iter-{iteration:02d}"
        result = VerificationResult(iteration=iteration, commit=commit)
        out = VerificationOutput(result=result)
        gates = result.gates

        ctx.log("gate static.syntax")
        gates.append(self.gate_syntax())
        ctx.log("gate static.residual")
        residual_gate, residual = self.gate_residual(prefix)
        gates.append(residual_gate)
        out.residual = residual
        gates.append(self.gate_scope())

        if target_env is None:
            gates.append(GateResult(gate="deps.target", title="Target dependencies", status="fail",
                                    summary=f"target environment could not be provisioned: {(env_error or '')[:600]}"))
            for gate, title in (("tests.pass", "Migrated tests pass"), ("tests.differential", "Differential tests"),
                                ("behavior.probes", "Behavioural probes")):
                gates.append(GateResult(gate=gate, title=title, status="fail", summary="not run: no target environment"))
            self._apply_ablation(result)
            return out

        ctx.log("running migrated test suite in sandbox")
        target_tests = ctx.runner.run_tests(target_env, ctx.target.path, ctx.scratch / prefix, "target")
        out.target_tests = target_tests
        _, sha = ctx.artifacts.save_json(f"{prefix}/target-tests.json", target_tests)
        artifacts = {"target-tests": sha}

        gates.append(self.gate_deps(target_env, target_tests))
        gates.append(self.gate_tests_pass(baseline.test_run, target_tests, artifacts))
        gates.append(self.gate_differential(baseline.test_run, target_tests, artifacts))
        gates.append(self.gate_integrity(baseline.test_run, target_tests))

        ctx.log("running ORIGINAL tests against migrated code (frozen oracle)")
        gates.append(self.gate_frozen(iteration, baseline.test_run, target_env))

        probe_run = None
        if ctx.config.verify.probes and baseline.probe_run is not None and baseline.probe_inputs:
            ctx.log("running differential behavioural probes")
            try:
                probe_run = ctx.runner.run_probes(target_env, ctx.target.path, ctx.scratch / prefix, "target",
                                                  ctx.artifacts.path(baseline.probe_inputs), discover=False)
                _, psha = ctx.artifacts.save_json(f"{prefix}/target-probes.json", probe_run)
                gates.append(self.gate_probes(baseline.probe_run, probe_run, {"target-probes": psha}))
            except Exception as exc:  # the runner itself failed: that is evidence too
                gates.append(GateResult(gate="behavior.probes", title="Behavioural probes", status="fail",
                                        summary=f"probe runner failed on the migrated code: {str(exc)[:500]}"))
        else:
            gates.append(GateResult(gate="behavior.probes", title="Behavioural probes", status="skip", blocking=False,
                                    summary="probes disabled or baseline probes unavailable"))
        out.probe_run = probe_run
        gates.append(self.gate_deprecations(target_tests, probe_run))
        self._apply_ablation(result)
        ctx.artifacts.save_json(f"{prefix}/verification.json", result)
        return out

    def _apply_ablation(self, result: VerificationResult) -> None:
        only = self.ctx.config.verify.only_gates
        if only is None:
            return
        for gate in result.gates:
            if gate.gate not in only:
                gate.blocking = False

    # ------------------------------------------------------------------ gates
    def gate_syntax(self) -> GateResult:
        errors = []
        root = self.ctx.target.path
        for py in iter_python_files(root):
            rel = py.relative_to(root).as_posix()
            try:
                compile(py.read_text(encoding="utf-8"), rel, "exec", dont_inherit=True)
            except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
                errors.append(f"{rel}: {exc}")
        errors = sorted(set(errors))
        return GateResult(gate="static.syntax", title="All Python files compile",
                          status="fail" if errors else "pass",
                          summary=f"{len(errors)} file(s) do not compile" if errors else "all files compile",
                          details={"errors": errors[:MAX_LIST]})

    def gate_residual(self, prefix: str) -> tuple[GateResult, list]:
        cfg = self.ctx.config.verify
        residual = self.ctx.recipe.residual(self.ctx.target.path, cfg.allowed_residual_rules)
        _, sha = self.ctx.artifacts.save_json(f"{prefix}/residual.json", [f.model_dump() for f in residual])
        status = "pass" if not residual else ("fail" if cfg.require_no_residual_v1_api else "warn")
        return (
            GateResult(
                gate="static.residual",
                title="No Pydantic v1 API left (dry-run codemod)",
                status=status,
                blocking=cfg.require_no_residual_v1_api,
                summary="no residual v1 usage" if not residual else f"{len(residual)} residual v1 usage(s)",
                details={"findings": [f"{f.path}:{f.line} {f.rule} {f.message}" for f in residual[:MAX_LIST]]},
                artifacts={"residual": sha},
            ),
            residual,
        )

    def gate_deps(self, env: ProvisionedEnv, tests: TestRunResult) -> GateResult:
        problems = []
        specs = declared_pydantic_specs(self.ctx.target.path)
        if not specs:
            problems.append("no pydantic requirement declared in any manifest")
        for file, spec in specs:
            req = pydantic_requirements([spec])[0]
            if spec_allows_v1(req.specifier):
                problems.append(f"{file}: '{spec}' still allows pydantic v1")
            if not spec_allows_v2(req.specifier):
                problems.append(f"{file}: '{spec}' does not allow pydantic v2")
        resolved = env.version_of("pydantic") or "?"
        if not resolved.startswith("2."):
            problems.append(f"resolved pydantic {resolved} is not 2.x")
        imported = tests.packages.get("pydantic")
        if imported and not imported.startswith("2."):
            problems.append(f"pydantic imported at test time is {imported}")
        return GateResult(gate="deps.target", title="Dependencies target pydantic v2",
                          status="fail" if problems else "pass",
                          summary="; ".join(problems) if problems else f"declared {', '.join(s for _, s in specs)}; resolved {resolved}",
                          details={"declared": specs, "resolved": resolved, "imported": imported, "lock": env.lock_hash,
                                   "packages": {k: v for k, v in env.packages.items() if "pydantic" in k}})

    def gate_tests_pass(self, baseline: TestRunResult, target: TestRunResult, artifacts: dict) -> GateResult:
        pre = {n for n, t in baseline.tests.items() if t.outcome in ("failed", "error")}
        failing = [t for t in target.failing if t.nodeid not in pre]
        problems = []
        if target.collection_errors:
            problems.append(f"{len(target.collection_errors)} collection error(s)")
        if not target.tests:
            problems.append("no tests were collected")
        if failing:
            problems.append(f"{len(failing)} failing test(s)")
        if target.timed_out:
            problems.append("test run timed out")
        counts = target.counts()
        return GateResult(
            gate="tests.pass", title="Migrated test suite passes",
            status="fail" if problems else "pass",
            summary="; ".join(problems) if problems else f"{counts.get('passed', 0)} passed",
            details={
                "counts": counts,
                "failing": [{"nodeid": t.nodeid, "outcome": t.outcome, "message": t.message[-1500:]} for t in failing[:12]],
                "collection_errors": [e[-2000:] for e in target.collection_errors[:5]],
                "preexisting_failures": sorted(pre)[:MAX_LIST],
                "stdout_tail": target.stdout_tail[-2000:] if problems else "",
            },
            artifacts=artifacts,
        )

    def gate_differential(self, baseline: TestRunResult, target: TestRunResult, artifacts: dict) -> GateResult:
        d = diff_tests(baseline, target)
        problems = []
        if d.regressions:
            problems.append(f"{len(d.regressions)} baseline-passing test(s) no longer pass")
        if d.missing:
            problems.append(f"{len(d.missing)} baseline test(s) missing")
        return GateResult(
            gate="tests.differential", title="Baseline-passing tests still pass",
            status="fail" if problems else "pass",
            summary="; ".join(problems) if problems else f"all {d.baseline_passed} baseline-passing tests pass",
            details={"regressions": [f"{n} -> {o}" for n, o in d.regressions[:MAX_LIST]], "missing": d.missing[:MAX_LIST],
                     "fixed": d.fixed[:MAX_LIST], "new_tests": d.new_tests[:MAX_LIST], "baseline_passed": d.baseline_passed},
            artifacts=artifacts,
        )

    def gate_integrity(self, baseline: TestRunResult, target: TestRunResult) -> GateResult:
        ctx = self.ctx
        protected = ctx.config.policy.protected
        violations: list[str] = []
        audited: list[str] = []
        for change in ctx.target.changed_files(ctx.base_commit):
            path = change.path
            name = path.rsplit("/", 1)[-1]
            is_protected = matches_any(path, protected) or is_test_path(path)
            if name in STARTUP_HOOK_NAMES or name.endswith(".pth"):
                if change.status != "M" or not self._audited(path):
                    violations.append(f"{change.status} {path}: start-up hook / test configuration file changed")
                    continue
            if not is_protected:
                continue
            if change.status == "D":
                violations.append(f"deleted protected file {path}")
            elif change.status in ("A", "R"):
                violations.append(f"{'added' if change.status == 'A' else 'renamed'} protected file {path}")
            elif change.status == "M":
                if self._audited(path):
                    audited.append(path)
                else:
                    before = ctx.baseline.path / path
                    after = ctx.target.path / path
                    note = ""
                    if before.is_file() and after.is_file() and path.endswith(".py"):
                        a = assertion_count(before.read_text(encoding="utf-8"))
                        b = assertion_count(after.read_text(encoding="utf-8"))
                        if b < a:
                            note = f" (assertions {a} -> {b})"
                    violations.append(f"protected file {path} modified beyond the audited codemod output{note}")
        for nodeid, added in new_skip_markers(baseline, target):
            violations.append(f"new {'/'.join(added)} marker on {nodeid}")
        pytest_cfg = self._pytest_config_changed()
        if pytest_cfg:
            violations.append(pytest_cfg)
        source_diff = ctx.target.diff(ctx.base_commit, unified=0)
        source_only = "\n".join(
            chunk for chunk in source_diff.split("diff --git ")
            if chunk.strip() and not is_test_path(chunk.split("\n", 1)[0].rsplit(" b/", 1)[-1].strip())
        )
        for why in suspicious_additions(source_only):
            violations.append(f"production code change {why}")
        return GateResult(
            gate="tests.integrity", title="Tests were not weakened",
            status="fail" if violations else "pass",
            summary="; ".join(violations[:3]) + (" ..." if len(violations) > 3 else "") if violations
            else f"{len(audited)} protected file(s) changed, all re-derivable from the audited codemod",
            details={"violations": violations[:MAX_LIST], "audited_codemod_changes": audited[:MAX_LIST]},
        )

    def _audited(self, path: str) -> bool:
        actual_path = self.ctx.target.path / path
        if not actual_path.is_file():
            return False
        actual = actual_path.read_text(encoding="utf-8")
        if not path.endswith(".py"):
            # dependency manifests (setup.cfg, pyproject.toml): only pydantic requirement
            # lines may differ from the original
            before = self.ctx.baseline.path / path
            if not before.is_file():
                return False
            return _strip_pydantic_lines(before.read_text(encoding="utf-8")) == _strip_pydantic_lines(actual)
        expected = self.ctx.recipe.expected_for(self.ctx.baseline.path, path, self.base_index)
        return expected is not None and actual == expected

    def _pytest_config_changed(self) -> str | None:
        def section(root: Path):
            p = root / "pyproject.toml"
            if not p.is_file():
                return None
            try:
                data = tomllib.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return "<unparseable>"
            return data.get("tool", {}).get("pytest")

        if section(self.ctx.baseline.path) != section(self.ctx.target.path):
            return "pyproject.toml [tool.pytest] configuration changed"
        return None

    def gate_frozen(self, iteration: int, baseline: TestRunResult, env: ProvisionedEnv) -> GateResult:
        ctx = self.ctx
        frozen = ctx.scratch / f"frozen-{iteration:02d}"
        shutil.rmtree(frozen, ignore_errors=True)
        shutil.copytree(ctx.target.path, frozen, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        replaced = 0
        for src in ctx.baseline.path.rglob("*"):
            rel = src.relative_to(ctx.baseline.path).as_posix()
            if rel.startswith(".git") or not src.is_file():
                continue
            if is_test_path(rel) or matches_any(rel, ctx.config.policy.protected):
                dest = frozen / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                replaced += 1
        run = ctx.runner.run_tests(env, frozen, ctx.scratch / f"iter-{iteration:02d}", "frozen")
        ctx.artifacts.save_json(f"iter-{iteration:02d}/frozen-tests.json", run)
        d = diff_tests(baseline, run)
        broken = [f"{n} -> {o}" for n, o in d.regressions] + [f"{n} -> missing" for n in d.missing]
        shutil.rmtree(frozen, ignore_errors=True)
        return GateResult(
            gate="tests.frozen", title="Original (unmodified) tests vs migrated code", blocking=False,
            status="warn" if broken or run.collection_errors else "pass",
            summary=(f"{len(broken)} original test(s) fail against the migrated code (often tests using removed v1 APIs)"
                     if broken else f"all {d.baseline_passed} original passing tests pass unmodified"),
            details={"broken": broken[:MAX_LIST], "collection_errors": [e[-800:] for e in run.collection_errors[:3]],
                     "files_restored": replaced},
        )

    def gate_probes(self, baseline: ProbeRunResult, target: ProbeRunResult, artifacts: dict) -> GateResult:
        cfg = self.ctx.config.verify
        diffs = diff_probes(baseline, target, cfg.accepted_behavior_changes, cfg.error_location_diff_blocking)
        blocking = [d for d in diffs if d.blocking]
        accepted = [d for d in diffs if d.accepted_reason]
        if not baseline.outcomes:
            return GateResult(gate="behavior.probes", title="Differential behavioural probes", status="skip",
                              blocking=False, summary="no probe-able models (settings classes are covered by tests only)",
                              details={"probes": 0, "import_errors": target.import_errors[:10]}, artifacts=artifacts)
        problems = []
        if target.import_errors:
            problems.append(f"{len(target.import_errors)} module(s) fail to import")
        if blocking:
            problems.append(f"{len(blocking)} behavioural difference(s)")
        return GateResult(
            gate="behavior.probes", title="Differential behavioural probes",
            status="fail" if problems else ("warn" if diffs else "pass"),
            summary="; ".join(problems) if problems
            else f"{len(baseline.outcomes)} probes agree" + (f" ({len(accepted)} accepted, {len(diffs) - len(accepted)} informational)" if diffs else ""),
            details={
                "probes": len(baseline.outcomes),
                "diffs": [d.model_dump() for d in blocking[:25]],
                "accepted": [d.model_dump() for d in accepted[:25]],
                "informational": [d.model_dump() for d in diffs if not d.blocking and not d.accepted_reason][:25],
                "import_errors": target.import_errors[:10],
                "pydantic": [baseline.pydantic_version, target.pydantic_version],
            },
            artifacts=artifacts,
        )

    def gate_deprecations(self, tests: TestRunResult, probes: ProbeRunResult | None) -> GateResult:
        warnings = list(tests.warnings) + (list(probes.import_warnings) if probes else [])
        found = project_deprecations(warnings)
        required = self.ctx.config.verify.require_no_deprecation_warnings
        return GateResult(
            gate="runtime.deprecations", title="No Pydantic deprecation warnings at runtime",
            status="pass" if not found else ("fail" if required else "warn"), blocking=required,
            summary="no deprecated API exercised" if not found else f"{len(found)} deprecated API use(s) observed at runtime",
            details={"warnings": found[:MAX_LIST]},
        )

    def gate_scope(self) -> GateResult:
        ctx = self.ctx
        writable = ctx.config.policy.writable
        protected = ctx.config.policy.protected
        problems = []
        for change in ctx.target.changed_files(ctx.base_commit):
            path = change.path
            if change.status == "D" and path.endswith(".py") and not is_test_path(path):
                problems.append(f"source module deleted: {path}")
                continue
            if matches_any(path, protected) or is_test_path(path):
                continue  # judged by tests.integrity
            if not matches_any(path, writable):
                problems.append(f"{change.status} {path}: outside the writable set")
        return GateResult(gate="scope.diff", title="Changes stay within the allowed scope",
                          status="fail" if problems else "pass",
                          summary="; ".join(problems[:3]) if problems else "all changes within writable paths",
                          details={"problems": problems[:MAX_LIST]})


_PYDANTIC_LINE = re.compile(r"pydantic", re.I)


def _strip_pydantic_lines(text: str) -> list[str]:
    return [line.rstrip() for line in text.splitlines() if not _PYDANTIC_LINE.search(line) and line.strip()]
