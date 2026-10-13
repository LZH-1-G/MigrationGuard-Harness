"""Pure comparison functions used by the evidence gates (no I/O, easy to unit test)."""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from typing import Any

from migrationguard.config import AcceptedBehaviorChange
from migrationguard.models import ProbeDiff, ProbeRunResult, RecordedWarning, TestRunResult


# --------------------------------------------------------------------------- tests
@dataclass
class TestDifferential:
    regressions: list[tuple[str, str]] = field(default_factory=list)  # (nodeid, target outcome)
    missing: list[str] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)
    preexisting_failures: list[str] = field(default_factory=list)
    new_tests: list[str] = field(default_factory=list)
    baseline_passed: int = 0

    @property
    def ok(self) -> bool:
        return not self.regressions and not self.missing


def diff_tests(baseline: TestRunResult, target: TestRunResult) -> TestDifferential:
    d = TestDifferential()
    for nodeid, base in sorted(baseline.tests.items()):
        tgt = target.tests.get(nodeid)
        if base.outcome == "passed":
            d.baseline_passed += 1
            if tgt is None:
                d.missing.append(nodeid)
            elif tgt.outcome != "passed":
                d.regressions.append((nodeid, tgt.outcome))
        elif base.outcome in ("failed", "error"):
            if tgt is not None and tgt.outcome == "passed":
                d.fixed.append(nodeid)
            else:
                d.preexisting_failures.append(nodeid)
    d.new_tests = sorted(set(target.tests) - set(baseline.tests))
    return d


def new_skip_markers(baseline: TestRunResult, target: TestRunResult) -> list[tuple[str, list[str]]]:
    out = []
    for nodeid, base in baseline.tests.items():
        tgt = target.tests.get(nodeid)
        if tgt is None:
            continue
        added = sorted(set(tgt.markers) - set(base.markers))
        if added:
            out.append((nodeid, added))
    return out


# --------------------------------------------------------------------------- warnings
_WS_MARKERS = ("/mg/workspace/", "/bind-workspace/")


def workspace_relative(filename: str) -> str | None:
    for marker in _WS_MARKERS:
        if marker in filename:
            return filename.split(marker, 1)[1]
    return None


def is_pydantic_deprecation(w: RecordedWarning) -> bool:
    category = w.category.lower()
    if "pydanticdeprecat" in category:
        return True
    return "deprecationwarning" in category and "pydantic" in w.message.lower()


def project_deprecations(warnings: list[RecordedWarning]) -> list[dict[str, Any]]:
    """Pydantic deprecation warnings attributable to project code."""
    seen: set[tuple[str, int, str]] = set()
    out = []
    for w in warnings:
        if not is_pydantic_deprecation(w):
            continue
        rel = workspace_relative(w.filename)
        if rel is None and not w.when.startswith("import:"):
            continue
        key = (rel or w.when, w.lineno, w.message[:80])
        if key in seen:
            continue
        seen.add(key)
        out.append({"location": f"{rel}:{w.lineno}" if rel else w.when, "message": w.message[:240], "category": w.category})
    return out


# --------------------------------------------------------------------------- probes
def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _accepted(probe_id: str, kind: str, rules: list[AcceptedBehaviorChange]) -> str | None:
    for rule in rules:
        if fnmatch.fnmatchcase(probe_id, rule.probe) or fnmatch.fnmatchcase(f"{probe_id}#{kind}", rule.probe):
            return rule.reason
    return None


def diff_probes(
    baseline: ProbeRunResult,
    target: ProbeRunResult,
    accepted: list[AcceptedBehaviorChange],
    error_loc_blocking: bool = False,
) -> list[ProbeDiff]:
    diffs: list[ProbeDiff] = []
    for pid, base in sorted(baseline.outcomes.items()):
        if pid.endswith("/generation-failed"):
            continue
        tgt = target.outcomes.get(pid)
        kind: str | None = None
        b_view: Any = None
        t_view: Any = None
        if tgt is None or tgt.status == "missing":
            kind = "missing"
            b_view, t_view = base.status, (tgt.error_type if tgt else "absent")
        elif base.status == "ok" and tgt.status == "ok":
            if _canon(base.value) != _canon(tgt.value):
                kind, b_view, t_view = "value", base.value, tgt.value
            elif base.json_value is not None and _canon(base.json_value) != _canon(tgt.json_value):
                kind, b_view, t_view = "json", base.json_value, tgt.json_value
        elif base.status == "ok" and tgt.status in ("error", "crash"):
            kind = "ok->error"
            b_view, t_view = base.value, {"error": tgt.error_type, "locs": tgt.error_locs}
        elif base.status in ("error",) and tgt.status == "ok":
            kind = "error->ok"
            b_view, t_view = {"error": base.error_type, "locs": base.error_locs}, tgt.value
        elif base.status == "error" and tgt.status == "error":
            if base.error_locs != tgt.error_locs:
                kind, b_view, t_view = "error-loc", base.error_locs, tgt.error_locs
        elif base.status == "crash" or tgt.status == "crash":
            if base.status != tgt.status or base.error_type != tgt.error_type:
                kind = "crash"
                b_view = {"status": base.status, "error": base.error_type}
                t_view = {"status": tgt.status, "error": tgt.error_type}
        if kind is None:
            continue
        reason = _accepted(pid, kind, accepted)
        blocking = reason is None and (kind != "error-loc" or error_loc_blocking)
        diffs.append(ProbeDiff(probe_id=pid, kind=kind, baseline=b_view, target=t_view, accepted_reason=reason, blocking=blocking))  # type: ignore[arg-type]
    return diffs


# --------------------------------------------------------------------------- integrity helpers
_ASSERT_LIKE = re.compile(r"^\s*(assert\b|with\s+pytest\.raises|pytest\.raises\(|self\.assert\w+\()", re.M)
_SUSPICIOUS = [
    (re.compile(r"PYTEST_CURRENT_TEST"), "inspects PYTEST_CURRENT_TEST"),
    (re.compile(r"[\"']pytest[\"']\s+in\s+sys\.modules"), "detects whether pytest is running"),
    (re.compile(r"sys\.modules\.get\(\s*[\"']_?pytest"), "detects whether pytest is running"),
    (re.compile(r"^\+\s*(import|from)\s+_?pytest\b", re.M), "production code imports pytest"),
    (re.compile(r"sys\._getframe|inspect\.stack\(\)"), "inspects the call stack"),
    (re.compile(r"MG_REPORT|mg_pytest_plugin|mg_probe_runner"), "references MigrationGuard internals"),
]


def assertion_count(source: str) -> int:
    return len(_ASSERT_LIKE.findall(source))


def suspicious_additions(diff_text: str) -> list[str]:
    added = "\n".join(line for line in diff_text.splitlines() if line.startswith("+") and not line.startswith("+++"))
    hits = []
    for pattern, why in _SUSPICIOUS:
        if pattern.search(added):
            hits.append(why)
    return sorted(set(hits))
