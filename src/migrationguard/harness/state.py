"""Run state, the explicit state machine, budget tracking and retry policy."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel, Field

from migrationguard.config import BudgetSection, RetrySection
from migrationguard.errors import BudgetExceeded, InvalidTransition, TransientError
from migrationguard.models import AgentUsage, Decision, MigrationPlan, Phase, ReviewVerdict

# --------------------------------------------------------------------------- state machine
TRANSITIONS: dict[Phase, set[Phase]] = {
    Phase.INIT: {Phase.ANALYZE},
    Phase.ANALYZE: {Phase.BASELINE},
    Phase.BASELINE: {Phase.PLAN},
    Phase.PLAN: {Phase.CODEMOD},
    Phase.CODEMOD: {Phase.VERIFY},
    Phase.VERIFY: {Phase.REVIEW, Phase.REPAIR, Phase.DECIDE},
    Phase.REPAIR: {Phase.VERIFY, Phase.DECIDE},
    Phase.REVIEW: {Phase.DECIDE, Phase.REPAIR},
    Phase.DECIDE: {Phase.ACCEPTED, Phase.REJECTED},
}
#: transitions allowed from any non-terminal phase
UNIVERSAL = {Phase.PAUSED, Phase.FAILED}


def check_transition(current: Phase, target: Phase) -> None:
    if current.terminal:
        raise InvalidTransition(f"run already finished ({current.value})")
    if current is Phase.PAUSED:
        raise InvalidTransition("a paused run must be resumed before it can transition")
    if target in UNIVERSAL or target in TRANSITIONS.get(current, set()):
        return
    raise InvalidTransition(f"illegal transition {current.value} -> {target.value}")


class VerificationRecord(BaseModel):
    iteration: int
    commit: str
    passed: bool
    failing_gates: list[str] = Field(default_factory=list)
    trigger: str = ""  # codemod | repair-N | review


class RunState(BaseModel):
    run_id: str
    repo: str
    run_dir: str
    branch: str
    base_commit: str
    phase: Phase = Phase.INIT
    paused_from: Phase | None = None
    pause_reason: str = ""
    failure_reason: str = ""
    iteration: int = 0
    repair_attempts: int = 0
    agent_backend: str = "none"
    sandbox_backend: str = ""
    sandbox_isolation: str = ""
    analysis_summary: dict[str, Any] = Field(default_factory=dict)
    plan: MigrationPlan | None = None
    baseline_artifact: str | None = None
    codemod_summary: dict[str, Any] = Field(default_factory=dict)
    last_verification: str | None = None  # artifact name
    history: list[VerificationRecord] = Field(default_factory=list)
    review: ReviewVerdict | None = None
    review_feedback_pending: bool = False
    decision: Decision | None = None
    usage: AgentUsage = Field(default_factory=AgentUsage)
    agent_calls: int = 0
    active_seconds: float = 0.0
    phase_retries: dict[str, int] = Field(default_factory=dict)
    commits: dict[str, str] = Field(default_factory=dict)
    stop_after: Phase | None = None
    options: dict[str, Any] = Field(default_factory=dict)

    def transition(self, target: Phase) -> None:
        check_transition(self.phase, target)
        if target is Phase.PAUSED:
            self.paused_from = self.phase
        self.phase = target


# --------------------------------------------------------------------------- budget
class BudgetTracker:
    """Enforces cost / token / call / wall-clock limits before every agent call."""

    def __init__(self, budget: BudgetSection, state: RunState):
        self.budget = budget
        self.state = state

    def check(self) -> None:
        s, b = self.state, self.budget
        if s.usage.cost_usd >= b.max_cost_usd:
            raise BudgetExceeded("cost_usd", s.usage.cost_usd, b.max_cost_usd)
        if s.usage.total_tokens >= b.max_tokens:
            raise BudgetExceeded("tokens", s.usage.total_tokens, b.max_tokens)
        if s.agent_calls >= b.max_agent_calls:
            raise BudgetExceeded("agent_calls", s.agent_calls, b.max_agent_calls)
        if s.active_seconds >= b.max_wall_seconds:
            raise BudgetExceeded("wall_seconds", s.active_seconds, b.max_wall_seconds)

    def remaining_usd(self) -> float:
        return max(0.0, self.budget.max_cost_usd - self.state.usage.cost_usd)

    def record(self, usage: AgentUsage) -> None:
        self.state.usage = self.state.usage + usage
        self.state.agent_calls += 1


# --------------------------------------------------------------------------- retries
T = TypeVar("T")


def with_retries(
    fn: Callable[[], T],
    policy: RetrySection,
    on_retry: Callable[[int, Exception, float], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    retry_on: tuple[type[BaseException], ...] = (TransientError,),
) -> T:
    """Run ``fn``; retry transient failures with exponential backoff and full jitter."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn()
        except retry_on as exc:  # type: ignore[misc]
            if attempt >= policy.max_attempts:
                raise
            delay = min(policy.max_delay_seconds, policy.base_delay_seconds * (2 ** (attempt - 1)))
            delay = random.uniform(delay / 2, delay)
            if on_retry:
                on_retry(attempt, exc, delay)  # type: ignore[arg-type]
            sleep(delay)
