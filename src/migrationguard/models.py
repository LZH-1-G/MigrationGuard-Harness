"""Domain models shared across the harness.

These are plain Pydantic v2 models so they serialise cleanly into checkpoints,
evidence records and reports.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Phase(str, enum.Enum):
    INIT = "init"
    ANALYZE = "analyze"
    BASELINE = "baseline"
    PLAN = "plan"
    CODEMOD = "codemod"
    VERIFY = "verify"
    REPAIR = "repair"
    REVIEW = "review"
    DECIDE = "decide"
    # terminal / suspended
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    FAILED = "failed"
    PAUSED = "paused"

    @property
    def terminal(self) -> bool:
        return self in (Phase.ACCEPTED, Phase.REJECTED, Phase.FAILED)


class Severity(str, enum.Enum):
    ERROR = "error"  # will break or is removed in v2
    WARNING = "warning"  # deprecated / behaviour changes in v2
    INFO = "info"  # possibly relevant, low confidence


class Finding(BaseModel):
    """A location in the code that needs (or may need) migration work."""

    model_config = ConfigDict(frozen=True)

    rule: str
    path: str
    line: int
    col: int = 0
    message: str
    severity: Severity = Severity.WARNING
    auto_fixable: bool = False
    snippet: str = ""

    def key(self) -> str:
        return f"{self.path}:{self.line}:{self.rule}"


class AppliedChange(BaseModel):
    rule: str
    path: str
    line: int
    description: str


class FileEdit(BaseModel):
    path: str
    before_sha256: str
    after_sha256: str
    changes: list[AppliedChange] = Field(default_factory=list)


class PlanStep(BaseModel):
    id: str
    kind: Literal["codemod", "dependency", "agent"]
    title: str
    detail: str = ""
    paths: list[str] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list)


class MigrationPlan(BaseModel):
    recipe: str
    steps: list[PlanStep] = Field(default_factory=list)
    unresolved: list[Finding] = Field(default_factory=list)

    @property
    def needs_agent(self) -> bool:
        return any(s.kind == "agent" for s in self.steps)


# --------------------------------------------------------------------- tests / probes
TestOutcomeKind = Literal["passed", "failed", "error", "skipped", "xfailed", "xpassed", "missing"]


class TestCaseResult(BaseModel):
    __test__ = False  # not a pytest test class
    nodeid: str
    outcome: TestOutcomeKind
    duration: float = 0.0
    markers: list[str] = Field(default_factory=list)
    message: str = ""


class RecordedWarning(BaseModel):
    category: str
    message: str
    filename: str
    lineno: int
    nodeid: str = ""
    when: str = ""


class TestRunResult(BaseModel):
    __test__ = False
    label: str
    exit_code: int
    collected: list[str] = Field(default_factory=list)
    tests: dict[str, TestCaseResult] = Field(default_factory=dict)
    collection_errors: list[str] = Field(default_factory=list)
    warnings: list[RecordedWarning] = Field(default_factory=list)
    packages: dict[str, str] = Field(default_factory=dict)
    duration_seconds: float = 0.0
    stdout_tail: str = ""
    timed_out: bool = False

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for t in self.tests.values():
            out[t.outcome] = out.get(t.outcome, 0) + 1
        return out

    @property
    def passed_ids(self) -> set[str]:
        return {k for k, v in self.tests.items() if v.outcome in ("passed", "xfailed")}

    @property
    def failing(self) -> list[TestCaseResult]:
        return [t for t in self.tests.values() if t.outcome in ("failed", "error")]


class ProbeOutcome(BaseModel):
    probe_id: str
    status: Literal["ok", "error", "missing", "crash"]
    value: Any = None
    json_value: Any = None
    error_locs: list[str] = Field(default_factory=list)
    error_type: str = ""


class ProbeRunResult(BaseModel):
    label: str
    outcomes: dict[str, ProbeOutcome] = Field(default_factory=dict)
    import_warnings: list[RecordedWarning] = Field(default_factory=list)
    import_errors: list[str] = Field(default_factory=list)
    pydantic_version: str = ""


class ProbeDiff(BaseModel):
    probe_id: str
    kind: Literal["ok->error", "error->ok", "value", "json", "error-loc", "missing", "crash"]
    baseline: Any = None
    target: Any = None
    accepted_reason: str | None = None
    blocking: bool = True


# --------------------------------------------------------------------- evidence
GateStatus = Literal["pass", "fail", "warn", "skip"]


class GateResult(BaseModel):
    gate: str
    title: str
    status: GateStatus
    blocking: bool = True
    summary: str
    details: dict[str, Any] = Field(default_factory=dict)
    artifacts: dict[str, str] = Field(default_factory=dict)  # name -> sha256

    @property
    def failed_blocking(self) -> bool:
        return self.status == "fail" and self.blocking


class VerificationResult(BaseModel):
    iteration: int
    commit: str
    gates: list[GateResult] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def passed(self) -> bool:
        return not any(g.failed_blocking for g in self.gates)

    def gate(self, name: str) -> GateResult | None:
        return next((g for g in self.gates if g.gate == name), None)

    def failing_gates(self) -> list[GateResult]:
        return [g for g in self.gates if g.failed_blocking]


class AgentClaim(BaseModel):
    """Something an agent asserted about its own work. Recorded, never trusted."""

    text: str
    verified: bool | None = None
    evidence: str = ""


class AgentUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0
    turns: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_creation_tokens

    def __add__(self, other: AgentUsage) -> AgentUsage:
        return AgentUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_creation_tokens=self.cache_creation_tokens + other.cache_creation_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
            turns=self.turns + other.turns,
        )


class AgentResult(BaseModel):
    role: Literal["repair", "reviewer"]
    status: Literal["finished", "turn_limit", "budget", "error"]
    summary: str = ""
    claims: list[AgentClaim] = Field(default_factory=list)
    usage: AgentUsage = Field(default_factory=AgentUsage)
    tool_calls: int = 0
    denied_tool_calls: int = 0
    files_written: list[str] = Field(default_factory=list)
    review: ReviewVerdict | None = None
    error: str = ""


class ReviewIssue(BaseModel):
    path: str = ""
    line: int = 0
    severity: Literal["blocker", "major", "minor"] = "major"
    explanation: str


class ReviewVerdict(BaseModel):
    verdict: Literal["approve", "reject"]
    summary: str
    issues: list[ReviewIssue] = Field(default_factory=list)


AgentResult.model_rebuild()


class Decision(BaseModel):
    outcome: Literal["accepted", "rejected", "failed"]
    reason: str
    blocking_gates: list[str] = Field(default_factory=list)
    final_commit: str = ""
    branch: str = ""
