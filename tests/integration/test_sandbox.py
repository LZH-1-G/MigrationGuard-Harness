from __future__ import annotations

import ast
import os
import stat

import pytest

from migrationguard.sandbox.base import Bind, ExecSpec
from migrationguard.sandbox.docker import DockerSandbox, default_image_tag
from migrationguard.sandbox.envs import find_python
from migrationguard.sandbox.local import LocalSandbox, _netns_available
from tests.conftest import uv_available

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not uv_available(), reason="uv not available")]

PROBE = """
import os, socket, sys
results = {}
try:
    open(os.path.join(sys.argv[1], "escape.txt"), "w").write("x")
    results["workspace_writable"] = True
except OSError:
    results["workspace_writable"] = False
try:
    open(os.path.join(sys.argv[2], "result.txt"), "w").write("ok")
    results["out_writable"] = True
except OSError:
    results["out_writable"] = False
try:
    socket.create_connection(("1.1.1.1", 53), timeout=2)
    results["network"] = True
except OSError:
    results["network"] = False
results["secret_visible"] = "ANTHROPIC_API_KEY" in os.environ or "MG_SECRET" in os.environ
print(results)
"""


def _spec(tmp_path):
    ws = tmp_path / "ws"
    out = tmp_path / "out"
    ws.mkdir()
    out.mkdir()
    (ws / "probe.py").write_text(PROBE)
    return ws, out, ExecSpec(
        argv=["${python}", "${workspace}/probe.py", "${workspace}", "${out}"],
        binds={"workspace": Bind(ws), "out": Bind(out, readonly=False)},
        timeout=60,
        interpreter=find_python("3.12"),
    )


def test_local_sandbox_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("MG_SECRET", "s3cret")
    ws, out, spec = _spec(tmp_path)
    result = LocalSandbox().run(spec)
    assert result.exit_code == 0, result.stderr
    data = ast.literal_eval(result.stdout.strip())
    assert data["out_writable"] and (out / "result.txt").exists()
    assert not (ws / "escape.txt").exists()  # the workspace is a throwaway copy
    assert not data["secret_visible"]
    if _netns_available():
        assert not data["network"]


def test_local_sandbox_timeout_kills_process(tmp_path):
    ws, _, spec = _spec(tmp_path)
    (ws / "probe.py").write_text("import time\ntime.sleep(30)\n")
    spec.timeout = 1
    result = LocalSandbox().run(spec)
    assert result.timed_out and result.duration < 10


def _docker() -> DockerSandbox:
    sb = DockerSandbox(default_image_tag("3.12"))
    ok, why = sb.check()
    if not ok:
        pytest.skip(f"docker sandbox unavailable: {why}")
    return sb


@pytest.mark.docker
def test_docker_sandbox_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("MG_SECRET", "s3cret")
    sb = _docker()
    ws, out, spec = _spec(tmp_path)
    result = sb.run(spec)
    assert result.exit_code == 0, result.stderr
    data = ast.literal_eval(result.stdout.strip())
    assert data == {"workspace_writable": False, "out_writable": True, "network": False, "secret_visible": False}
    assert not (ws / "escape.txt").exists() and (out / "result.txt").read_text() == "ok"


@pytest.mark.docker
def test_docker_sandbox_cannot_write_rootfs_and_runs_unprivileged(tmp_path):
    sb = _docker()
    ws, _, spec = _spec(tmp_path)
    (ws / "probe.py").write_text(
        "import os\n"
        "try:\n    open('/usr/evil', 'w'); print('rootfs writable')\nexcept OSError: print('rootfs read-only')\n"
        "print('uid', os.getuid())\n"
    )
    result = sb.run(spec)
    assert "rootfs read-only" in result.stdout
    assert os.getuid() != 0
    assert f"uid {os.getuid()}" in result.stdout


@pytest.mark.docker
def test_docker_preserves_host_output_permissions(tmp_path):
    sb = _docker()
    _, out, spec = _spec(tmp_path)
    out.chmod(0o700)
    mode_before = stat.S_IMODE(out.stat().st_mode)
    result = sb.run(spec)
    assert result.exit_code == 0, result.stderr
    assert (out / "result.txt").read_text() == "ok"
    assert stat.S_IMODE(out.stat().st_mode) == mode_before == 0o700
