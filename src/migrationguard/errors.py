"""Exception hierarchy.

The orchestrator classifies failures by type: :class:`TransientError` is retried with
backoff, :class:`BudgetExceeded` and :class:`RunInterrupted` pause the run (resumable),
everything else fails the run with the error recorded as evidence.
"""

from __future__ import annotations


class MigrationGuardError(Exception):
    """Base class for all MigrationGuard errors."""


class ConfigError(MigrationGuardError):
    pass


class RepositoryError(MigrationGuardError):
    pass


class TransientError(MigrationGuardError):
    """A failure that is expected to succeed on retry (timeouts, API hiccups)."""


class SandboxError(MigrationGuardError):
    pass


class SandboxUnavailable(SandboxError):
    pass


class EnvironmentProvisionError(TransientError):
    """Dependency resolution / installation failed (often network related)."""


class PolicyViolation(MigrationGuardError):
    """An agent tried to perform an action the permission policy forbids."""


class BudgetExceeded(MigrationGuardError):
    def __init__(self, what: str, used: float, limit: float):
        super().__init__(f"budget exceeded: {what} used={used:g} limit={limit:g}")
        self.what = what
        self.used = used
        self.limit = limit


class AgentQuotaExceeded(MigrationGuardError):
    """The model provider refused service for quota reasons (usage/session limit).

    Retrying immediately is pointless; the run is paused and can be resumed after the reset.
    """


class RunInterrupted(MigrationGuardError):
    """The run was stopped deliberately (Ctrl-C, --stop-after); it can be resumed."""


class InvalidTransition(MigrationGuardError):
    pass


class AgentUnavailable(MigrationGuardError):
    pass
