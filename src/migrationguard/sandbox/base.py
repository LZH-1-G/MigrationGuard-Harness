"""Sandbox abstraction.

An :class:`ExecSpec` describes a command in terms of *named binds* rather than concrete
paths. Arguments and environment values may reference ``${<bind>}`` and ``${python}``;
each backend resolves them to its own paths (container paths for Docker, host paths or
throwaway copies for the local backend). This keeps the verification code identical for
both backends.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

_PLACEHOLDER = re.compile(r"\$\{([a-z_]+)\}")


@dataclass
class Bind:
    host: Path
    readonly: bool = True
    #: copy the directory before execution (local backend) so the run cannot modify it
    snapshot: bool = False


@dataclass
class ExecSpec:
    argv: list[str]
    binds: dict[str, Bind]
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = "workspace"
    timeout: int = 900
    network: bool = False
    label: str = "exec"
    #: host interpreter (local backend); docker uses the image's python3
    interpreter: Path | None = None


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    duration: float
    timed_out: bool
    backend: str

    def tail(self, chars: int = 4000) -> str:
        text = (self.stdout + "\n" + self.stderr).strip()
        return text[-chars:]


class Sandbox(ABC):
    name: str = "sandbox"
    #: human readable description of the isolation guarantees, recorded in reports
    isolation: str = ""

    @abstractmethod
    def run(self, spec: ExecSpec) -> ExecResult: ...

    @abstractmethod
    def check(self) -> tuple[bool, str]:
        """Return (usable, explanation)."""

    @staticmethod
    def substitute(value: str, mapping: dict[str, str]) -> str:
        return _PLACEHOLDER.sub(lambda m: mapping.get(m.group(1), m.group(0)), value)
