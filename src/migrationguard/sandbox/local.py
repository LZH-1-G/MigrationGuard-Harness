"""Process-level sandbox (fallback when Docker is unavailable).

Guarantees (weaker than Docker, and reported as such):

* the workspace is **copied** to a throwaway directory before every run, so test code
  cannot modify the worktree under verification;
* the environment is scrubbed (no API keys, tokens, proxies, user site-packages);
  ``HOME`` and ``TMPDIR`` point at a temporary directory;
* network is disabled with an unprivileged network namespace (``unshare -rn``) when the
  kernel allows it;
* CPU time, address space and file size are limited with rlimits; the whole process
  group is killed on timeout.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
from functools import lru_cache
from pathlib import Path

from migrationguard.sandbox.base import ExecResult, ExecSpec, Sandbox

_IGNORE = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".pytest_cache", ".mypy_cache", ".venv", "venv")


@lru_cache(maxsize=1)
def _netns_available() -> bool:
    unshare = shutil.which("unshare")
    if not unshare:
        return False
    try:
        proc = subprocess.run([unshare, "-rn", "true"], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _parse_memory(value: str) -> int:
    value = value.strip().lower()
    units = {"k": 1024, "m": 1024**2, "g": 1024**3}
    if value and value[-1] in units:
        return int(float(value[:-1]) * units[value[-1]])
    return int(value)


class LocalSandbox(Sandbox):
    name = "local"

    def __init__(self, memory: str = "2g", cpu_seconds: int = 1800, allow_network: bool = False):
        self.memory_bytes = _parse_memory(memory) * 2  # address space > RSS for CPython
        self.cpu_seconds = cpu_seconds
        self.allow_network = allow_network

    @property
    def isolation(self) -> str:  # type: ignore[override]
        net = "network namespace (no network)" if _netns_available() and not self.allow_network else "NO network isolation"
        return f"process sandbox: copied workspace, scrubbed env, rlimits, {net}"

    def check(self) -> tuple[bool, str]:
        return True, self.isolation

    def run(self, spec: ExecSpec) -> ExecResult:
        with tempfile.TemporaryDirectory(prefix="mg-local-") as tmp:
            tmpdir = Path(tmp)
            mapping: dict[str, str] = {}
            for name, bind in spec.binds.items():
                if bind.snapshot or (name == "workspace" and bind.readonly):
                    dest = tmpdir / f"bind-{name}"
                    shutil.copytree(bind.host, dest, ignore=_IGNORE, symlinks=True)
                    mapping[name] = str(dest)
                else:
                    mapping[name] = str(bind.host)
            home = tmpdir / "home"
            home.mkdir()
            mapping["python"] = str(spec.interpreter or "python3")
            argv = [self.substitute(a, mapping) for a in spec.argv]
            env = {
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": str(home),
                "TMPDIR": str(home),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1",
                "PYTHONHASHSEED": "0",
                "PYTHONUNBUFFERED": "1",
            }
            env.update({k: self.substitute(v, mapping) for k, v in spec.env.items()})
            if not (spec.network or self.allow_network) and _netns_available():
                argv = ["unshare", "-rn", *argv]
            cwd = mapping.get(spec.cwd, str(tmpdir))
            start = time.monotonic()
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
                preexec_fn=self._limits,
            )
            timed_out = False
            try:
                stdout, stderr = proc.communicate(timeout=spec.timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                stdout, stderr = proc.communicate()
            return ExecResult(
                exit_code=proc.returncode if not timed_out else -9,
                stdout=stdout,
                stderr=stderr,
                duration=time.monotonic() - start,
                timed_out=timed_out,
                backend=self.name,
            )

    def _limits(self) -> None:  # pragma: no cover - runs in the child
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (self.cpu_seconds, self.cpu_seconds))
        resource.setrlimit(resource.RLIMIT_AS, (self.memory_bytes, self.memory_bytes))
        resource.setrlimit(resource.RLIMIT_FSIZE, (512 * 1024**2, 512 * 1024**2))
