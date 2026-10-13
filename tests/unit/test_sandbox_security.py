"""No-Docker regressions for isolation defaults and writable bind handling."""

from __future__ import annotations

import os
import stat
import subprocess

import pytest

from migrationguard.config import MigrationGuardConfig
from migrationguard.errors import SandboxError, SandboxUnavailable
from migrationguard.sandbox.base import Bind, ExecSpec
from migrationguard.sandbox.docker import DockerSandbox, _host_unprivileged_user
from migrationguard.sandbox.runner import make_sandbox


def test_default_sandbox_requires_docker(monkeypatch):
    assert MigrationGuardConfig().sandbox.backend == "docker"
    monkeypatch.setattr(DockerSandbox, "check", lambda self: (False, "docker unavailable"))
    with pytest.raises(SandboxUnavailable, match="docker unavailable"):
        make_sandbox(MigrationGuardConfig())


def test_host_root_is_not_mapped_to_container_root(monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: 0)
    with pytest.raises(SandboxError, match="host root"):
        _host_unprivileged_user()


def test_writable_mount_never_changes_host_permissions(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: 1234)
    monkeypatch.setattr(os, "getgid", lambda: 5678)
    monkeypatch.setattr("migrationguard.sandbox.docker.docker_binary", lambda: "docker")
    calls = []

    def fake_docker_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr("migrationguard.sandbox.docker.subprocess.run", fake_docker_run)
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    before = stat.S_IMODE(out.stat().st_mode)
    spec = ExecSpec(argv=["true"], binds={"out": Bind(out, readonly=False)})
    result = DockerSandbox("unit-test-image").run(spec)
    assert result.exit_code == 0
    assert stat.S_IMODE(out.stat().st_mode) == before == 0o700
    assert calls[0][calls[0].index("--user"):][:2] == ["--user", "1234:5678"]
    assert "type=bind,source=" in " ".join(calls[0])
