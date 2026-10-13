"""Docker sandbox and runtime-image builder.

Every command runs in a fresh container:

``--network none`` · ``--read-only`` root filesystem · tmpfs ``/tmp`` · all capabilities
dropped · ``no-new-privileges`` · pids/memory/cpu limits · unprivileged uid · the
workspace and dependency site mounted **read-only**; only the ``out`` bind is writable.

Runtime images are tagged ``migrationguard/runtime:py<version>``. Two build strategies:

``dockerfile``  ``FROM python:<v>-slim`` (needs registry access).
``standalone``  ``FROM scratch`` + a uv-managed standalone CPython and the glibc
                libraries it links against, copied from the host. Needs no registry,
                which makes it work behind restrictive egress policies.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from migrationguard.errors import SandboxError, SandboxUnavailable
from migrationguard.sandbox.base import ExecResult, ExecSpec, Sandbox
from migrationguard.sandbox.envs import find_python

CONTAINER_ROOT = "/mg"
SANDBOX_UID = "65534:65534"


def docker_binary() -> str | None:
    return shutil.which("docker")


def docker_daemon_ok() -> tuple[bool, str]:
    exe = docker_binary()
    if not exe:
        return False, "docker CLI not found"
    try:
        proc = subprocess.run([exe, "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"docker not responding: {exc}"
    if proc.returncode != 0:
        return False, (proc.stderr.strip().splitlines() or ["docker daemon unavailable"])[-1]
    return True, f"docker {proc.stdout.strip()}"


def image_exists(tag: str) -> bool:
    exe = docker_binary()
    if not exe:
        return False
    return subprocess.run([exe, "image", "inspect", tag], capture_output=True).returncode == 0


class DockerSandbox(Sandbox):
    name = "docker"

    def __init__(self, image: str, memory: str = "2g", cpus: float = 2.0, pids_limit: int = 512, allow_network: bool = False):
        self.image = image
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.allow_network = allow_network

    @property
    def isolation(self) -> str:  # type: ignore[override]
        net = "bridge network" if self.allow_network else "no network"
        return (
            f"docker ({self.image}): {net}, read-only rootfs, cap-drop ALL, no-new-privileges, "
            f"uid {SANDBOX_UID}, mem {self.memory}, cpus {self.cpus}, pids {self.pids_limit}"
        )

    def check(self) -> tuple[bool, str]:
        ok, why = docker_daemon_ok()
        if not ok:
            return False, why
        if not image_exists(self.image):
            return False, f"runtime image {self.image} missing (run `migrationguard sandbox build-image`)"
        return True, self.isolation

    def run(self, spec: ExecSpec) -> ExecResult:
        exe = docker_binary()
        if not exe:
            raise SandboxUnavailable("docker CLI not found")
        name = f"mg-{spec.label}-{uuid.uuid4().hex[:10]}"
        name = re.sub(r"[^a-zA-Z0-9_.-]", "-", name)
        mapping = {bind: f"{CONTAINER_ROOT}/{bind}" for bind in spec.binds}
        mapping["python"] = "python3"
        cmd = [
            exe, "run", "--rm", "--name", name,
            "--network", "bridge" if (spec.network or self.allow_network) else "none",
            "--read-only",
            "--tmpfs", "/tmp:rw,exec,nosuid,size=1g,mode=1777",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", str(self.pids_limit),
            "--memory", self.memory,
            "--cpus", str(self.cpus),
            "--user", SANDBOX_UID,
            "-w", mapping.get(spec.cwd, "/tmp"),
            "-e", "HOME=/tmp", "-e", "TMPDIR=/tmp", "-e", "LANG=C.UTF-8",
            "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTHONNOUSERSITE=1",
            "-e", "PYTHONHASHSEED=0", "-e", "PYTHONUNBUFFERED=1",
            "-e", "PATH=/usr/local/bin:/usr/bin:/bin",
        ]
        for key, value in spec.env.items():
            cmd += ["-e", f"{key}={self.substitute(value, mapping)}"]
        for bind_name, bind in spec.binds.items():
            host = bind.host.resolve()
            if not bind.readonly:
                os.chmod(host, 0o777)
            mode = "ro" if bind.readonly else "rw"
            cmd += ["--mount", f"type=bind,source={host},target={mapping[bind_name]},{'readonly' if mode == 'ro' else ''}".rstrip(",")]
        cmd.append(self.image)
        cmd += [self.substitute(a, mapping) for a in spec.argv]
        start = time.monotonic()
        timed_out = False
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=spec.timeout)
            stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            subprocess.run([exe, "kill", name], capture_output=True)
            stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            code = -9
        if code == 125:  # docker itself failed (bad mount, missing image...)
            raise SandboxError(f"docker run failed: {stderr.strip()[-800:]}")
        return ExecResult(code, stdout, stderr, time.monotonic() - start, timed_out, self.name)


# --------------------------------------------------------------------------- image builder
def default_image_tag(python: str) -> str:
    return f"migrationguard/runtime:py{python}"


def build_runtime_image(python: str, tag: str | None = None, strategy: str = "auto", log=print) -> str:
    """Build the sandbox runtime image. Returns the tag."""
    tag = tag or default_image_tag(python)
    ok, why = docker_daemon_ok()
    if not ok:
        raise SandboxUnavailable(why)
    errors = []
    strategies = ["dockerfile", "standalone"] if strategy == "auto" else [strategy]
    for strat in strategies:
        try:
            if strat == "dockerfile":
                _build_from_dockerfile(python, tag)
            elif strat == "standalone":
                _build_standalone(python, tag)
            else:
                raise SandboxError(f"unknown strategy {strat}")
            log(f"built {tag} with strategy '{strat}'")
            return tag
        except SandboxError as exc:
            errors.append(f"{strat}: {exc}")
            log(f"strategy '{strat}' failed: {str(exc)[:300]}")
    raise SandboxError("could not build runtime image:\n" + "\n".join(errors))


def _docker_build(context: Path, tag: str, python: str) -> None:
    proc = subprocess.run(
        ["docker", "build", "-q", "-t", tag, "--label", f"org.migrationguard.python={python}", str(context)],
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if proc.returncode != 0:
        raise SandboxError(proc.stderr.strip()[-1500:] or "docker build failed")


def _build_from_dockerfile(python: str, tag: str) -> None:
    with tempfile.TemporaryDirectory(prefix="mg-image-") as tmp:
        context = Path(tmp)
        (context / "Dockerfile").write_text(
            f"FROM python:{python}-slim\n"
            "ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1\n"
            "USER 65534:65534\n"
            "WORKDIR /tmp\n",
            encoding="utf-8",
        )
        _docker_build(context, tag, python)


def _ldd(path: Path) -> list[Path]:
    proc = subprocess.run(["ldd", str(path)], capture_output=True, text=True)
    libs = []
    for line in proc.stdout.splitlines():
        match = re.search(r"(/\S+)\s+\(0x", line)
        if match:
            libs.append(Path(match.group(1)))
    return libs


def _build_standalone(python: str, tag: str) -> None:
    interpreter = find_python(python)
    real = interpreter.resolve()
    prefix = real.parent.parent  # .../cpython-3.12.x-linux-x86_64-gnu
    if not (prefix / "lib").is_dir():
        raise SandboxError(f"unexpected interpreter layout at {prefix}")
    with tempfile.TemporaryDirectory(prefix="mg-image-") as tmp:
        context = Path(tmp)
        rootfs = context / "rootfs"
        (rootfs / "opt").mkdir(parents=True)
        shutil.copytree(prefix, rootfs / "opt" / "python", symlinks=True,
                        ignore=shutil.ignore_patterns("test", "tests", "idlelib", "tkinter", "turtledemo", "__pycache__"))
        bindir = rootfs / "usr" / "local" / "bin"
        bindir.mkdir(parents=True)
        exe_name = real.name
        for link in ("python3", "python", f"python{python}"):
            (bindir / link).symlink_to(f"/opt/python/bin/{exe_name}")
        # shared libraries: interpreter + extension modules + libgcc_s (rust/C++ wheels)
        needed: set[Path] = set(_ldd(real))
        for so in (prefix / "lib").rglob("*.so"):
            needed.update(_ldd(so))
        for extra in ("libgcc_s.so.1", "libstdc++.so.6", "libz.so.1"):
            for libdir in ("/lib/x86_64-linux-gnu", "/usr/lib/x86_64-linux-gnu", "/lib/aarch64-linux-gnu", "/lib64", "/usr/lib64"):
                candidate = Path(libdir) / extra
                if candidate.exists():
                    needed.add(candidate)
                    break
        for lib in sorted(needed):
            if not lib.exists() or str(lib).startswith(str(prefix)):
                continue
            for src in {lib, lib.resolve()}:
                dest = rootfs / str(src).lstrip("/")
                dest.parent.mkdir(parents=True, exist_ok=True)
                if not dest.exists():
                    shutil.copy2(src, dest)
        (rootfs / "etc").mkdir()
        (rootfs / "etc" / "passwd").write_text("root:x:0:0:root:/root:/bin/false\nnobody:x:65534:65534:nobody:/tmp:/bin/false\n")
        (rootfs / "etc" / "group").write_text("root:x:0:\nnogroup:x:65534:\n")
        (rootfs / "tmp").mkdir(mode=0o1777)
        (context / "Dockerfile").write_text(
            "FROM scratch\n"
            "COPY rootfs/ /\n"
            "ENV PATH=/usr/local/bin:/usr/bin:/bin LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1\n"
            "USER 65534:65534\n"
            "WORKDIR /tmp\n"
            'CMD ["python3"]\n',
            encoding="utf-8",
        )
        _docker_build(context, tag, python)
    # smoke test
    proc = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", tag, "python3", "-c", "import ssl, sqlite3, json, decimal; print('ok')"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0 or "ok" not in proc.stdout:
        raise SandboxError(f"standalone image smoke test failed: {proc.stderr.strip()[-800:]}")
