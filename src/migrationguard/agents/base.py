"""Agent backend interface.

The harness talks to agents only through :class:`AgentBackend`. An agent receives an
:class:`AgentTask` (prompt + a policy-guarded toolbox) and returns an
:class:`~migrationguard.models.AgentResult`. Whatever the result *claims* is recorded and
then ignored by the acceptance decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from migrationguard.agents.tools import AgentToolbox
from migrationguard.models import AgentResult


@dataclass
class AgentTask:
    role: str  # "repair" | "reviewer"
    system_prompt: str
    prompt: str
    toolbox: AgentToolbox
    worktree: Path
    max_turns: int
    max_budget_usd: float
    model: str | None = None
    iteration: int = 0
    base_commit: str = ""


class AgentBackend(Protocol):
    name: str

    def repair(self, task: AgentTask) -> AgentResult: ...

    def review(self, task: AgentTask) -> AgentResult: ...


class NullAgent:
    """Used when no agent is configured: deterministic-only migration."""

    name = "none"

    def repair(self, task: AgentTask) -> AgentResult:
        return AgentResult(role="repair", status="error", error="no agent backend configured")

    def review(self, task: AgentTask) -> AgentResult:
        return AgentResult(role="reviewer", status="error", error="no agent backend configured")
