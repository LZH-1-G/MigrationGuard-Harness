"""Reproducible environment provisioning with uv.

Each environment is resolved to a fully pinned lock (``uv pip compile``), then installed
with ``uv pip install --target`` into a *content-addressed* site directory
(``<home>/envs/<python>/<sha256(lock)>``). The sandbox mounts that directory read-only
and puts it on ``PYTHONPATH``, so:

* the test run itself never needs network access;
* baseline and migrated environments are cached and shared across runs and benchmarks;
* the exact lock file is recorded as evidence.

Resolution happens on the host (it needs the package index); execution happens in the
sandbox.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from migrationguard.errors import EnvironmentProvisionError, SandboxError

_LOCK_LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==([^\s;\\]+)")


def uv_binary() -> str:
    try:
        from uv import find_uv_bin  # type: ignore[import-not-found]

        return find_uv_bin()
    except Exception:
        found = shutil.which("uv")
        if not found:
            raise SandboxError("uv is required (pip install uv)") from None
        return found


def _uv(args: list[str], timeout: int = 900) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.setdefault("UV_NO_PROGRESS", "1")
    try:
        # run outside any project directory so uv never discovers a project venv
        return subprocess.run([uv_binary(), *args], capture_output=True, text=True, timeout=timeout, env=env,
                              cwd=tempfile.gettempdir())
    except subprocess.TimeoutExpired as exc:
        raise EnvironmentProvisionError(f"uv {' '.join(args[:3])} timed out") from exc


def find_python(version: str, install: bool = True) -> Path:
    """Locate (and if necessary install) a CPython interpreter for ``version``."""
    # --system: never pick up a virtualenv (it would leak its site-packages into the sandbox)
    proc = _uv(["python", "find", "--system", "--managed-python", version])
    if proc.returncode != 0:
        proc = _uv(["python", "find", "--system", version])
    if proc.returncode != 0 and install:
        inst = _uv(["python", "install", version])
        if inst.returncode != 0:
            raise EnvironmentProvisionError(f"cannot install Python {version}: {inst.stderr.strip()[-500:]}")
        proc = _uv(["python", "find", "--system", version])
    if proc.returncode != 0:
        raise EnvironmentProvisionError(f"Python {version} not found: {proc.stderr.strip()[-500:]}")
    return Path(proc.stdout.strip().splitlines()[-1])


@dataclass
class EnvSpec:
    label: str
    python: str
    requirements: list[str]
    constraints: list[str] = field(default_factory=list)
    exclude_newer: str | None = None


@dataclass
class ProvisionedEnv:
    label: str
    python_version: str
    interpreter: Path
    site_dir: Path
    lock_text: str
    lock_hash: str
    packages: dict[str, str]
    cached: bool = False

    def version_of(self, package: str) -> str | None:
        key = package.lower().replace("_", "-")
        return self.packages.get(key)


class EnvironmentProvisioner:
    def __init__(self, cache_root: Path):
        self.cache_root = cache_root
        self.cache_root.mkdir(parents=True, exist_ok=True)

    def provision(self, spec: EnvSpec) -> ProvisionedEnv:
        interpreter = find_python(spec.python)
        with tempfile.TemporaryDirectory(prefix="mg-resolve-") as tmp:
            tmpdir = Path(tmp)
            req_in = tmpdir / "requirements.in"
            req_in.write_text("\n".join(spec.requirements) + "\n", encoding="utf-8")
            args = ["pip", "compile", str(req_in), "--python", str(interpreter), "--quiet", "--no-header",
                    "--no-annotate", "-o", str(tmpdir / "lock.txt")]
            if spec.constraints:
                constraints = tmpdir / "constraints.txt"
                constraints.write_text("\n".join(spec.constraints) + "\n", encoding="utf-8")
                args += ["-c", str(constraints)]
            if spec.exclude_newer:
                args += ["--exclude-newer", spec.exclude_newer]
            proc = _uv(args)
            if proc.returncode != 0:
                message = (proc.stderr or proc.stdout).strip()
                raise EnvironmentProvisionError(f"dependency resolution failed for {spec.label}: {message[-1500:]}")
            lock_text = (tmpdir / "lock.txt").read_text(encoding="utf-8")
        lock_hash = hashlib.sha256(f"{spec.python}\n{lock_text}".encode()).hexdigest()[:24]
        site_dir = self.cache_root / f"py{spec.python}" / lock_hash
        packages = parse_lock(lock_text)
        marker = site_dir / ".mg-complete"
        if marker.exists():
            return ProvisionedEnv(spec.label, spec.python, interpreter, site_dir, lock_text, lock_hash, packages, cached=True)
        staging = Path(tempfile.mkdtemp(prefix=f"{lock_hash}-", dir=self.cache_root))
        try:
            lock_file = staging.parent / f"{lock_hash}.lock.txt"
            lock_file.write_text(lock_text, encoding="utf-8")
            proc = _uv(
                ["pip", "install", "--quiet", "--python", str(interpreter), "--target", str(staging / "site"),
                 "--no-deps", "-r", str(lock_file), "--link-mode", "copy", "--compile-bytecode"]
            )
            if proc.returncode != 0:
                raise EnvironmentProvisionError(
                    f"installing {spec.label} environment failed: {(proc.stderr or proc.stdout).strip()[-1500:]}"
                )
            site_dir.parent.mkdir(parents=True, exist_ok=True)
            if site_dir.exists():
                shutil.rmtree(site_dir)
            (staging / "site").rename(site_dir)
            (site_dir / "mg-lock.txt").write_text(lock_text, encoding="utf-8")
            _make_world_readable(site_dir)
            marker.write_text("ok\n", encoding="utf-8")
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return ProvisionedEnv(spec.label, spec.python, interpreter, site_dir, lock_text, lock_hash, packages)


def parse_lock(lock_text: str) -> dict[str, str]:
    packages: dict[str, str] = {}
    for line in lock_text.splitlines():
        match = _LOCK_LINE.match(line.strip())
        if match:
            packages[match.group(1).lower().replace("_", "-")] = match.group(2)
    return packages


def _make_world_readable(root: Path) -> None:
    """The docker sandbox runs as an unprivileged uid: site dirs must be readable."""
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            p = os.path.join(dirpath, name)
            os.chmod(p, os.stat(p).st_mode | 0o555)
        for name in filenames:
            p = os.path.join(dirpath, name)
            os.chmod(p, os.stat(p).st_mode | 0o444)
    os.chmod(root, os.stat(root).st_mode | 0o555)
