from __future__ import annotations

from migrationguard.config import AcceptedBehaviorChange
from migrationguard.models import ProbeOutcome, ProbeRunResult, RecordedWarning, TestCaseResult, TestRunResult
from migrationguard.verify.compare import (
    assertion_count,
    diff_probes,
    diff_tests,
    new_skip_markers,
    project_deprecations,
    suspicious_additions,
)


def run(label, **outcomes):
    return TestRunResult(label=label, exit_code=0, tests={
        k: TestCaseResult(nodeid=k, outcome=v[0] if isinstance(v, tuple) else v, markers=list(v[1]) if isinstance(v, tuple) else [])
        for k, v in outcomes.items()
    })


def test_diff_tests_regressions_missing_and_preexisting():
    base = run("b", a="passed", b="passed", c="failed", d="passed")
    target = run("t", a="passed", b="skipped", c="failed", e="passed")
    d = diff_tests(base, target)
    assert d.regressions == [("b", "skipped")]
    assert d.missing == ["d"]
    assert d.preexisting_failures == ["c"]
    assert d.new_tests == ["e"]
    assert not d.ok


def test_new_skip_markers():
    base = run("b", a="passed")
    target = run("t", a=("skipped", ["skip"]))
    assert new_skip_markers(base, target) == [("a", ["skip"])]


def probes(label, **outcomes):
    return ProbeRunResult(label=label, outcomes={k: ProbeOutcome(probe_id=k, **v) for k, v in outcomes.items()})


def test_diff_probes_kinds_and_acceptance():
    base = probes("b",
                  **{"m:A/full": {"status": "ok", "value": {"x": 1}, "json_value": {"x": 1}},
                     "m:A/json": {"status": "ok", "value": {"d": 1}, "json_value": {"d": 1.5}},
                     "m:A/coerce-str:x": {"status": "ok", "value": {"x": "1"}},
                     "m:A/missing:x": {"status": "error", "error_locs": ["x"]},
                     "m:A/locs": {"status": "error", "error_locs": ["a"]},
                     "m:B/full": {"status": "ok", "value": {}}})
    target = probes("t",
                    **{"m:A/full": {"status": "ok", "value": {"x": 1}, "json_value": {"x": 1}},
                       "m:A/json": {"status": "ok", "value": {"d": 1}, "json_value": {"d": "1.5"}},
                       "m:A/coerce-str:x": {"status": "error", "error_locs": ["x"]},
                       "m:A/missing:x": {"status": "ok", "value": {}},
                       "m:A/locs": {"status": "error", "error_locs": ["a", "b"]}})
    accepted = [AcceptedBehaviorChange(probe="m:*/coerce-str:*", reason="ok by humans")]
    diffs = {d.probe_id: d for d in diff_probes(base, target, accepted)}
    assert "m:A/full" not in diffs
    assert diffs["m:A/json"].kind == "json" and diffs["m:A/json"].blocking
    assert diffs["m:A/coerce-str:x"].kind == "ok->error" and not diffs["m:A/coerce-str:x"].blocking
    assert diffs["m:A/coerce-str:x"].accepted_reason == "ok by humans"
    assert diffs["m:A/missing:x"].kind == "error->ok" and diffs["m:A/missing:x"].blocking
    assert diffs["m:A/locs"].kind == "error-loc" and not diffs["m:A/locs"].blocking
    assert diffs["m:B/full"].kind == "missing" and diffs["m:B/full"].blocking


def test_project_deprecations_only_counts_project_code():
    warnings = [
        RecordedWarning(category="pydantic.warnings.PydanticDeprecatedSince20", message="dict is deprecated",
                        filename="/mg/workspace/app/m.py", lineno=3),
        RecordedWarning(category="pydantic.warnings.PydanticDeprecatedSince20", message="dict is deprecated",
                        filename="/mg/workspace/app/m.py", lineno=3),
        RecordedWarning(category="pydantic.warnings.PydanticDeprecatedSince20", message="x",
                        filename="/mg/site/somelib/x.py", lineno=1),
        RecordedWarning(category="builtins.DeprecationWarning", message="unrelated", filename="/mg/workspace/a.py", lineno=1),
        RecordedWarning(category="pydantic.warnings.PydanticDeprecatedSince20", message="class-based config",
                        filename="/x/pydantic/_internal/_config.py", lineno=1, when="import:app.models"),
    ]
    found = project_deprecations(warnings)
    assert [f["location"] for f in found] == ["app/m.py:3", "import:app.models"]


def test_suspicious_additions():
    diff = '+import sys\n+if "pytest" in sys.modules:\n+    pass\n-old\n+x = os.environ.get("PYTEST_CURRENT_TEST")\n'
    hits = suspicious_additions(diff)
    assert "detects whether pytest is running" in hits and "inspects PYTEST_CURRENT_TEST" in hits
    assert suspicious_additions("+x = 1\n") == []


def test_assertion_count():
    assert assertion_count("def t():\n    assert a\n    with pytest.raises(E):\n        f()\n") == 2
