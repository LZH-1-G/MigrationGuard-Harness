from __future__ import annotations

import pytest

from migrationguard.bench import strategies
from migrationguard.bench.cases import load_cases
from migrationguard.bench.runner import BenchConfig, BenchResults, render_results, run_one, summarize
from tests.conftest import uv_available

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not uv_available(), reason="uv not available")]


def test_cases_are_well_formed():
    cases = load_cases()
    assert {c.name for c in cases} >= {"inventory", "billing", "settings_service", "catalog"}
    for case in cases:
        assert (case.repo / "tests").is_dir() and list(case.oracle.glob("test_*.py")), case.name


def test_ground_truth_distinguishes_correct_and_incorrect(tmp_path, mg_home):
    cfg = BenchConfig(out=tmp_path / "bench", home=mg_home)
    billing = load_cases(["billing"])[0]
    naive = run_one(billing, strategies.get("codemod-only"), cfg, print)
    # "CI is green" accepts the codemod output, but the hidden oracle shows it is wrong
    assert naive.accepted and naive.complete and not naive.behavior_ok and not naive.correct
    strict = run_one(billing, strategies.get("migrationguard-deterministic"), cfg, print)
    assert not strict.accepted and "behavior.probes" in strict.failing_gates

    results = BenchResults(rows=[naive, strict])
    summary = summarize([naive])
    assert summary["false_acceptances"] == 1 and summary["false_acceptance_rate"] == 1.0
    assert "codemod-only" in render_results(results)
