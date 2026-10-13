"""Benchmark strategies.

Every strategy is executed by the *same* harness (sandbox, environments, evidence), so the
only differences are what each strategy does and which evidence it uses to accept:

=============================  ===========  ==========  ==================================
strategy                       codemods     agent       acceptance rule
=============================  ===========  ==========  ==================================
bump-only                      no (pin)     none        migrated tests pass ("CI green")
codemod-only                   yes          none        migrated tests pass ("CI green")
migrationguard-deterministic   yes          none        all evidence gates
claude-tests-only              yes          Claude      tests pass + no regressions
claude-no-codemod              tests only   Claude      all evidence gates + reviewer
migrationguard                 yes          Claude      all evidence gates + reviewer
adv-<name>                     yes          red-team    all evidence gates (+ counterfactual
                                                        tests-only acceptance)
=============================  ===========  ==========  ==================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from migrationguard.agents.scripted import ADVERSARIES

TESTS_ONLY = ["tests.pass"]
TESTS_AND_DIFF = ["tests.pass", "tests.differential"]


@dataclass
class Strategy:
    name: str
    description: str
    codemods: bool | str = True
    agent: str = "none"  # none | claude | adversary:<name>
    reviewer: bool = False
    probes: bool = True
    only_gates: list[str] | None = None
    max_repairs: int = 3
    uses_llm: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def overrides(self) -> dict[str, Any]:
        return {"verify": {"only_gates": self.only_gates, "reviewer": self.reviewer, "probes": self.probes}}


STRATEGIES: dict[str, Strategy] = {
    "bump-only": Strategy("bump-only", "Bump the pin to pydantic v2, change no code; accept when the test suite is green.",
                          codemods=False, only_gates=TESTS_ONLY),
    "codemod-only": Strategy("codemod-only", "Deterministic codemods (bump-pydantic style); accept when the test suite is green.",
                             only_gates=TESTS_ONLY),
    "migrationguard-deterministic": Strategy("migrationguard-deterministic",
                                             "Codemods + every MigrationGuard evidence gate, no LLM."),
    "claude-tests-only": Strategy("claude-tests-only",
                                  "Codemods + Claude repair loop driven and accepted by tests only (typical coding-agent setup).",
                                  agent="claude", only_gates=TESTS_AND_DIFF, probes=False, uses_llm=True),
    "claude-no-codemod": Strategy("claude-no-codemod",
                                  "Claude migrates all source code (codemods only on protected test files); full gates + reviewer.",
                                  codemods="tests", agent="claude", reviewer=True, uses_llm=True),
    "migrationguard": Strategy("migrationguard", "Full MigrationGuard: codemods + Claude repair + evidence gates + independent reviewer.",
                               agent="claude", reviewer=True, uses_llm=True),
}
for _name in ADVERSARIES:
    STRATEGIES[f"adv-{_name}"] = Strategy(
        f"adv-{_name}",
        f"Red-team agent '{_name}' (claims success) against the full evidence gates; max 1 repair.",
        agent=f"adversary:{_name}", reviewer=True, max_repairs=1,
    )


def get(name: str) -> Strategy:
    if name == "adversaries":
        raise KeyError("use the expanded list")
    try:
        return STRATEGIES[name]
    except KeyError:
        raise KeyError(f"unknown strategy {name!r}; available: {sorted(STRATEGIES)}") from None


def expand(names: list[str]) -> list[str]:
    out: list[str] = []
    for name in names:
        if name == "adversaries":
            out.extend(n for n in STRATEGIES if n.startswith("adv-"))
        elif name == "all-free":
            out.extend(n for n, s in STRATEGIES.items() if not s.uses_llm)
        else:
            get(name)
            out.append(name)
    return out
