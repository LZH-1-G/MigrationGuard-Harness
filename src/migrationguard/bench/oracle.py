"""Ground truth for benchmark runs — independent of the harness's own gates.

``correct`` = ``behavior_ok`` and ``complete`` where

behavior_ok  every test that passes on the original project also passes on the migrated
             source. The test set is the case's **hidden oracle** plus the **reference**
             visible tests: the original test files transformed by the deterministic codemod
             *recomputed from the baseline* (so an agent that edited, skipped or deleted
             tests gains nothing: its test files are discarded).
complete     no Pydantic v1 API (incl. the ``pydantic.v1`` shim) remains in the migrated
             tree, the manifests require pydantic v2 only, and the resolved version is 2.x.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from migrationguard.analysis.project import ProjectIndex, is_test_path, matches_any
from migrationguard.bench.cases import BenchCase
from migrationguard.config import MigrationGuardConfig
from migrationguard.migrate.deps import (
    collect_requirements,
    declared_pydantic_specs,
    pydantic_requirements,
    spec_allows_v1,
    spec_allows_v2,
)
from migrationguard.migrate.recipe import PydanticV2Recipe
from migrationguard.models import TestRunResult
from migrationguard.sandbox.envs import EnvironmentProvisioner, EnvSpec
from migrationguard.sandbox.runner import SandboxRunner

ORACLE_DIR = "mg_hidden_oracle"
_IGNORE = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".pytest_cache")


@dataclass
class GroundTruth:
    behavior_ok: bool
    complete: bool
    oracle_total: int = 0
    reference_total: int = 0
    failures: list[str] = field(default_factory=list)
    incomplete_reasons: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def correct(self) -> bool:
        return self.behavior_ok and self.complete


def _copy_tree(src: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, ignore=_IGNORE)


def _test_like(rel: str, config: MigrationGuardConfig) -> bool:
    return is_test_path(rel) or matches_any(rel, config.policy.protected)


def build_eval_tree(case: BenchCase, source_tree: Path, baseline_tree: Path, dest: Path, config: MigrationGuardConfig,
                    transform: bool) -> None:
    """``source_tree`` minus its tests + reference tests from ``baseline_tree`` + hidden oracle."""
    _copy_tree(source_tree, dest)
    for path in sorted(dest.rglob("*")):
        rel = path.relative_to(dest).as_posix()
        if path.is_file() and _test_like(rel, config) and rel != "migrationguard.toml":
            path.unlink()
    recipe = PydanticV2Recipe(config.migration.target_requirement, config.migration.settings_requirement)
    index = ProjectIndex.build(baseline_tree) if transform else None
    for path in sorted(baseline_tree.rglob("*")):
        rel = path.relative_to(baseline_tree).as_posix()
        if rel.startswith(".git") or not path.is_file() or not _test_like(rel, config) or rel == "migrationguard.toml":
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if transform and rel.endswith(".py"):
            target.write_text(recipe.expected_for(baseline_tree, rel, index) or "", encoding="utf-8")
        elif transform and rel.rsplit("/", 1)[-1] in ("setup.cfg", "pyproject.toml"):
            continue  # keep the migrated manifest (dependency pins legitimately changed)
        else:
            shutil.copy2(path, target)
    oracle_dest = dest / ORACLE_DIR
    oracle_dest.mkdir(exist_ok=True)
    for test in sorted(case.oracle.glob("*.py")):
        shutil.copy2(test, oracle_dest / test.name)


def evaluate(
    case: BenchCase,
    target_tree: Path,
    baseline_tree: Path,
    config: MigrationGuardConfig,
    provisioner: EnvironmentProvisioner,
    runner: SandboxRunner,
    scratch: Path,
) -> GroundTruth:
    scratch.mkdir(parents=True, exist_ok=True)
    recipe = PydanticV2Recipe(config.migration.target_requirement, config.migration.settings_requirement)
    extra = config.environment.extra_requirements

    # ---- completeness
    reasons: list[str] = []
    residual = recipe.residual(target_tree)
    if residual:
        reasons.append(f"{len(residual)} residual v1 usage(s): " + "; ".join(f"{f.path}:{f.line} {f.rule}" for f in residual[:5]))
    specs = declared_pydantic_specs(target_tree)
    for file, spec in specs:
        req = pydantic_requirements([spec])[0]
        if spec_allows_v1(req.specifier) or not spec_allows_v2(req.specifier):
            reasons.append(f"{file}: {spec} is not a pydantic-v2-only requirement")
    if not specs:
        reasons.append("no pydantic requirement declared")

    # ---- behaviour
    base_eval = scratch / "eval-baseline"
    target_eval = scratch / "eval-target"
    build_eval_tree(case, baseline_tree, baseline_tree, base_eval, config, transform=False)
    build_eval_tree(case, target_tree, baseline_tree, target_eval, config, transform=True)
    base_env = provisioner.provision(EnvSpec("oracle-baseline", config.environment.python,
                                             collect_requirements(baseline_tree) + extra, ["pydantic<2"]))
    try:
        target_env = provisioner.provision(EnvSpec("oracle-target", config.environment.python,
                                                   collect_requirements(target_tree) + extra))
    except Exception as exc:  # unresolvable manifests: behaviour cannot be established
        return GroundTruth(False, False, failures=[f"target environment: {exc}"], incomplete_reasons=reasons)
    if not (target_env.version_of("pydantic") or "").startswith("2."):
        reasons.append(f"resolved pydantic {target_env.version_of('pydantic')}")

    base_run = runner.run_tests(base_env, base_eval, scratch / "oracle-runs", "oracle-baseline", extra_args=["."])
    target_run = runner.run_tests(target_env, target_eval, scratch / "oracle-runs", "oracle-target", extra_args=["."])
    reference = _passing(base_run)
    oracle_ids = {n for n in reference if n.startswith(ORACLE_DIR)}
    if not oracle_ids:
        return GroundTruth(False, not reasons, failures=["hidden oracle does not pass on the original project"],
                           incomplete_reasons=reasons, error="broken case")
    failures = []
    for nodeid in sorted(reference):
        result = target_run.tests.get(nodeid)
        if result is None or result.outcome != "passed":
            failures.append(f"{nodeid} -> {result.outcome if result else 'missing'}")
    failures.extend(f"collection: {e[-300:]}" for e in target_run.collection_errors[:3])
    return GroundTruth(
        behavior_ok=not failures,
        complete=not reasons,
        oracle_total=len(oracle_ids),
        reference_total=len(reference) - len(oracle_ids),
        failures=failures,
        incomplete_reasons=reasons,
    )


def _passing(run: TestRunResult) -> set[str]:
    return {n for n, t in run.tests.items() if t.outcome == "passed"}
