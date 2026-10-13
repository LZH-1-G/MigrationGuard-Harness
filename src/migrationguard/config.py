"""Configuration model for MigrationGuard.

Configuration is read from ``migrationguard.toml`` in the repository root, or from
``[tool.migrationguard]`` in ``pyproject.toml``. Every field has a safe default, so a
repository without any configuration can still be migrated.

The configuration file is itself a *protected* path: agents can never edit it. That
matters because it is where humans record accepted behaviour changes.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

CONFIG_FILENAME = "migrationguard.toml"


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MigrationSection(_Section):
    recipe: Literal["pydantic-v1-to-v2"] = "pydantic-v1-to-v2"
    #: Requirement written into dependency files for the migrated project.
    target_requirement: str = "pydantic>=2.7,<3"
    #: Requirement used when ``BaseSettings`` has to move to ``pydantic-settings``.
    settings_requirement: str = "pydantic-settings>=2.2,<3"


class EnvironmentSection(_Section):
    #: Python version used for both the baseline and the migrated environment.
    python: str = "3.12"
    #: Requirement files to install. Empty = auto-detect (requirements*.txt / pyproject).
    requirements: list[str] = Field(default_factory=list)
    #: Extra requirements installed into both environments (test tooling etc.).
    extra_requirements: list[str] = Field(default_factory=lambda: ["pytest>=7,<9"])
    #: Directories (relative to repo root) placed on PYTHONPATH. Empty = auto (src/ + root).
    pythonpath: list[str] = Field(default_factory=list)
    #: Arguments passed to ``python -m pytest`` (paths, -k expressions are NOT allowed to change).
    pytest_args: list[str] = Field(default_factory=lambda: ["-q"])
    #: Only resolve packages uploaded before this date (reproducible resolution).
    exclude_newer: str | None = None
    #: Packages whose resolved versions are recorded as evidence.
    track_packages: list[str] = Field(default_factory=lambda: ["pydantic", "pydantic-core", "pydantic-settings"])


class SandboxSection(_Section):
    backend: Literal["auto", "docker", "local"] = "docker"
    image: str = "migrationguard/runtime:py{python}"
    timeout_seconds: int = 900
    memory: str = "2g"
    cpus: float = 2.0
    pids_limit: int = 512
    #: Allow network inside the sandbox during test execution (default: no network).
    network: bool = False


class PolicySection(_Section):
    #: Paths agents must never modify. Deterministic codemods may change test files, but
    #: only with transformations that are re-derivable (audited) from the original file.
    protected: list[str] = Field(
        default_factory=lambda: [
            "tests/**",
            "test/**",
            "**/test_*.py",
            "**/*_test.py",
            "**/conftest.py",
            "conftest.py",
            "pytest.ini",
            "tox.ini",
            "setup.cfg",
            "noxfile.py",
            ".github/**",
            ".gitlab-ci.yml",
            "migrationguard.toml",
            "migrationguard_probes.py",
        ]
    )
    #: Paths agents may write (subject to ``protected``).
    writable: list[str] = Field(
        default_factory=lambda: ["**/*.py", "requirements*.txt", "pyproject.toml", "setup.py"]
    )
    #: Maximum size of a single file an agent may write, in bytes.
    max_write_bytes: int = 200_000


class AcceptedBehaviorChange(_Section):
    """A behaviour difference a human has explicitly accepted."""

    #: Probe identifier pattern (fnmatch), e.g. ``app.models:Invoice/coerce-int:*``.
    probe: str
    reason: str


class VerifySection(_Section):
    require_no_residual_v1_api: bool = True
    require_no_deprecation_warnings: bool = True
    probes: bool = True
    max_probe_inputs_per_model: int = 14
    #: User probe module (functions named ``probe_*``) at repo root.
    probe_module: str = "migrationguard_probes.py"
    accepted_behavior_changes: list[AcceptedBehaviorChange] = Field(default_factory=list)
    #: Error-location differences are informational by default (v2 reports union errors differently).
    error_location_diff_blocking: bool = False
    reviewer: bool = True
    #: Residual rule ids that are tolerated (not blocking).
    allowed_residual_rules: list[str] = Field(default_factory=list)
    #: Ablation: if set, only these gates can block acceptance (all others become advisory).
    #: Used by benchmark strategies to emulate simpler acceptance policies ("CI is green").
    only_gates: list[str] | None = None


class AgentSection(_Section):
    backend: Literal["claude", "none"] = "claude"
    model: str | None = None
    reviewer_model: str | None = None
    max_repair_iterations: int = 3
    max_turns: int = 60
    max_tool_test_runs: int = 12
    #: Max characters of evidence put directly into a prompt (the agent can pull more via tools).
    prompt_evidence_chars: int = 24_000


class BudgetSection(_Section):
    max_cost_usd: float = 10.0
    max_tokens: int = 4_000_000
    max_agent_calls: int = 12
    max_wall_seconds: int = 3_600


class RetrySection(_Section):
    max_attempts: int = 3
    base_delay_seconds: float = 2.0
    max_delay_seconds: float = 30.0


class MigrationGuardConfig(_Section):
    migration: MigrationSection = Field(default_factory=MigrationSection)
    environment: EnvironmentSection = Field(default_factory=EnvironmentSection)
    sandbox: SandboxSection = Field(default_factory=SandboxSection)
    policy: PolicySection = Field(default_factory=PolicySection)
    verify: VerifySection = Field(default_factory=VerifySection)
    agent: AgentSection = Field(default_factory=AgentSection)
    budget: BudgetSection = Field(default_factory=BudgetSection)
    retry: RetrySection = Field(default_factory=RetrySection)

    @field_validator("environment")
    @classmethod
    def _python_version(cls, env: EnvironmentSection) -> EnvironmentSection:
        parts = env.python.split(".")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            raise ValueError("environment.python must look like '3.12'")
        return env

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls, repo: Path, overrides: dict[str, Any] | None = None) -> MigrationGuardConfig:
        data: dict[str, Any] = {}
        toml_path = repo / CONFIG_FILENAME
        pyproject = repo / "pyproject.toml"
        if toml_path.is_file():
            data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        elif pyproject.is_file():
            raw = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            data = raw.get("tool", {}).get("migrationguard", {}) or {}
        if overrides:
            data = deep_merge(data, overrides)
        return cls.model_validate(data)

    def image_for(self) -> str:
        return self.sandbox.image.format(python=self.environment.python)


def _drop_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _drop_none(v) for k, v in value.items() if v is not None}
    return value


def deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """Merge ``extra`` into ``base``; ``None`` values in ``extra`` mean "not set"."""
    out = dict(base)
    for key, value in _drop_none(extra).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        elif isinstance(value, dict) and not value:
            continue
        else:
            out[key] = value
    return out


def home_dir() -> Path:
    """Directory holding the run database, run artifacts, worktrees and env cache."""
    root = os.environ.get("MIGRATIONGUARD_HOME")
    path = Path(root).expanduser() if root else Path.home() / ".migrationguard"
    path.mkdir(parents=True, exist_ok=True)
    return path


DEFAULT_TOML = """\
# MigrationGuard configuration. Agents can never modify this file.

[migration]
recipe = "pydantic-v1-to-v2"
target_requirement = "pydantic>=2.7,<3"

[environment]
python = "{python}"
# requirements = ["requirements.txt"]       # auto-detected when empty
extra_requirements = ["pytest>=7,<9"]
pytest_args = ["-q"]

[sandbox]
backend = "docker"        # safe default; local/auto require explicit opt-in
timeout_seconds = 900

[verify]
probes = true
reviewer = true
# Behaviour changes a human has reviewed and accepted:
# accepted_behavior_changes = [
#   {{ probe = "app.models:Item/coerce-int:*", reason = "v2 no longer truncates floats" }},
# ]

[agent]
backend = "claude"        # claude | none
max_repair_iterations = 3

[budget]
max_cost_usd = 10.0
max_tokens = 4000000
"""
