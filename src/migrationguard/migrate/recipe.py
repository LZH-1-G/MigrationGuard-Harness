"""The Pydantic v1 → v2 migration recipe.

A recipe bundles four deterministic capabilities the harness relies on:

* ``analyze``  – findings for the original tree (dry-run codemod + dependency scan)
* ``plan``     – an explicit, inspectable migration plan
* ``apply``    – run codemods + dependency rewrites on a worktree
* ``residual`` – dry-run over a migrated tree; anything left is evidence of an incomplete
  migration
* ``expected_for`` – what the codemod would produce for an original file; used to audit
  changes to protected (test) files
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from migrationguard.analysis.project import ProjectIndex, is_test_path
from migrationguard.migrate import deps as depmod
from migrationguard.migrate.pydantic_codemod import (
    RULE_DOCS,
    CodemodReport,
    file_needs_codemod,
    migrate_source,
)
from migrationguard.models import AppliedChange, FileEdit, Finding, MigrationPlan, PlanStep, Severity


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class AnalysisResult:
    index: ProjectIndex
    findings: list[Finding] = field(default_factory=list)
    reports: dict[str, CodemodReport] = field(default_factory=dict)
    pydantic_specs: list[tuple[str, str]] = field(default_factory=list)
    needs_settings_dependency: bool = False
    parse_errors: dict[str, str] = field(default_factory=dict)

    @property
    def auto_fixable(self) -> list[Finding]:
        return [f for f in self.findings if f.auto_fixable]

    @property
    def unresolved(self) -> list[Finding]:
        return [f for f in self.findings if not f.auto_fixable and f.severity is not Severity.INFO]

    def summary(self) -> dict[str, object]:
        by_rule: dict[str, int] = defaultdict(int)
        for f in self.findings:
            by_rule[f.rule] += 1
        return {
            "python_files": len(self.index.modules),
            "models": len(self.index.models()),
            "findings": len(self.findings),
            "auto_fixable": len(self.auto_fixable),
            "unresolved": len(self.unresolved),
            "by_rule": dict(sorted(by_rule.items())),
            "pydantic_specs": self.pydantic_specs,
        }


@dataclass
class ApplyResult:
    file_edits: list[FileEdit] = field(default_factory=list)
    dependency_edits: list[depmod.DependencyEdit] = field(default_factory=list)
    unresolved: list[Finding] = field(default_factory=list)
    needs_settings_dependency: bool = False


class PydanticV2Recipe:
    name = "pydantic-v1-to-v2"

    def __init__(self, target_requirement: str = "pydantic>=2.7,<3", settings_requirement: str = "pydantic-settings>=2.2,<3"):
        self.target_requirement = target_requirement
        self.settings_requirement = settings_requirement

    # ------------------------------------------------------------------ analysis
    def analyze(self, root: Path) -> AnalysisResult:
        index = ProjectIndex.build(root)
        result = AnalysisResult(index=index, parse_errors=dict(index.parse_errors))
        for relpath in index.modules:
            source = (root / relpath).read_text(encoding="utf-8")
            if not file_needs_codemod(source, index):
                continue
            _, report = migrate_source(source, relpath, index)
            result.reports[relpath] = report
            result.findings.extend(report.findings())
            result.needs_settings_dependency |= report.needs_settings_dependency
        result.pydantic_specs = depmod.declared_pydantic_specs(root)
        for file, spec in result.pydantic_specs:
            req = depmod.pydantic_requirements([spec])
            if req and not depmod.spec_allows_v2(req[0].specifier):
                result.findings.append(
                    Finding(
                        rule="DEP001",
                        path=file,
                        line=0,
                        message=f"dependency '{spec}' excludes pydantic v2",
                        severity=Severity.ERROR,
                        auto_fixable=True,
                    )
                )
        return result

    def plan(self, analysis: AnalysisResult) -> MigrationPlan:
        plan = MigrationPlan(recipe=self.name)
        dep_findings = [f for f in analysis.findings if f.rule.startswith("DEP")]
        settings = " + add pydantic-settings" if analysis.needs_settings_dependency else ""
        plan.steps.append(
            PlanStep(
                id="deps",
                kind="dependency",
                title=f"Pin {self.target_requirement}{settings}",
                detail="Rewrite pydantic requirements in every dependency manifest.",
                paths=sorted({f.path for f in dep_findings}) or [p for p, _ in analysis.pydantic_specs],
            )
        )
        by_rule: dict[str, list[Finding]] = defaultdict(list)
        for f in analysis.auto_fixable:
            if not f.rule.startswith("DEP"):
                by_rule[f.rule].append(f)
        for rule, items in sorted(by_rule.items()):
            plan.steps.append(
                PlanStep(
                    id=f"codemod-{rule}",
                    kind="codemod",
                    title=f"{rule}: {RULE_DOCS.get(rule, rule)}",
                    detail=f"{len(items)} site(s)",
                    paths=sorted({f.path for f in items}),
                    rules=[rule],
                )
            )
        by_file: dict[str, list[Finding]] = defaultdict(list)
        for f in analysis.unresolved:
            by_file[f.path].append(f)
        for path, items in sorted(by_file.items()):
            plan.steps.append(
                PlanStep(
                    id=f"agent-{path}",
                    kind="agent",
                    title=f"Manual migration in {path}",
                    detail="; ".join(f"L{f.line} {f.rule}: {f.message}" for f in items)[:2000],
                    paths=[path],
                    rules=sorted({f.rule for f in items}),
                )
            )
        plan.unresolved = analysis.unresolved
        return plan

    # ------------------------------------------------------------------ application
    def apply(self, root: Path, code: bool = True, tests_only: bool = False) -> ApplyResult:
        """Apply codemods and rewrite dependency manifests.

        ``code=False`` only bumps the dependency. ``tests_only=True`` applies the codemod to
        test files only (the audited transformation agents are not allowed to perform).
        """
        index = ProjectIndex.build(root)
        result = ApplyResult()
        for relpath in index.modules if code else []:
            if tests_only and not is_test_path(relpath):
                continue
            path = root / relpath
            source = path.read_text(encoding="utf-8")
            if not file_needs_codemod(source, index):
                continue
            new_source, report = migrate_source(source, relpath, index)
            result.unresolved.extend(f for f in report.unresolved)
            result.needs_settings_dependency |= report.needs_settings_dependency
            if new_source != source:
                path.write_text(new_source, encoding="utf-8")
                result.file_edits.append(
                    FileEdit(
                        path=relpath,
                        before_sha256=sha256_text(source),
                        after_sha256=sha256_text(new_source),
                        changes=report.changes,
                    )
                )
        dep_report = depmod.rewrite_dependencies(
            root,
            self.target_requirement,
            self.settings_requirement if result.needs_settings_dependency else None,
        )
        result.dependency_edits = dep_report.edits
        return result

    # ------------------------------------------------------------------ verification helpers
    def residual(self, root: Path, allowed_rules: list[str] | None = None) -> list[Finding]:
        """Findings that remain in a (supposedly) migrated tree."""
        allowed = set(allowed_rules or [])
        analysis = self.analyze(root)
        return [
            f
            for f in analysis.findings
            if f.severity is not Severity.INFO and f.rule not in allowed
        ]

    def expected_for(self, base_root: Path, relpath: str, base_index: ProjectIndex | None = None) -> str | None:
        """The codemod's output for ``relpath`` in the original tree (None if absent)."""
        path = base_root / relpath
        if not path.is_file():
            return None
        source = path.read_text(encoding="utf-8")
        if not relpath.endswith(".py"):
            return source
        index = base_index or ProjectIndex.build(base_root)
        if not file_needs_codemod(source, index):
            return source
        new_source, _ = migrate_source(source, relpath, index)
        return new_source


def summarize_changes(changes: list[AppliedChange]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for c in changes:
        out[c.rule] += 1
    return dict(sorted(out.items()))


def split_test_and_source(paths: list[str]) -> tuple[list[str], list[str]]:
    tests = [p for p in paths if is_test_path(p)]
    return tests, [p for p in paths if p not in tests]
