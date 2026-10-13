"""Dependency installation must not execute source-distribution build hooks on the host."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from migrationguard.sandbox import envs


def test_environment_provisioning_is_wheel_only(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(envs, "find_python", lambda version: Path(sys.executable))

    def uv_stub(args, timeout=900):
        calls.append(args)
        if args[:2] == ["pip", "compile"]:
            Path(args[args.index("-o") + 1]).write_text("pydantic==2.14.1\n")
        elif args[:2] == ["pip", "install"]:
            Path(args[args.index("--target") + 1]).mkdir(parents=True)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(envs, "_uv", uv_stub)
    provisioned = envs.EnvironmentProvisioner(tmp_path / "envs").provision(
        envs.EnvSpec(label="target", python="3.12", requirements=["pydantic>=2,<3"])
    )
    assert provisioned.version_of("pydantic") == "2.14.1"
    assert [args[0:2] for args in calls] == [["pip", "compile"], ["pip", "install"]]
    assert all("--no-build" in args for args in calls)
