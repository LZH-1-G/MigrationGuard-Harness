"""High-level sandboxed operations: run the test suite, run behavioural probes.

These functions are the only way verification code executes project code.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from migrationguard.config import MigrationGuardConfig
from migrationguard.errors import SandboxError, SandboxUnavailable
from migrationguard.models import ProbeOutcome, ProbeRunResult, RecordedWarning, TestCaseResult, TestRunResult
from migrationguard.sandbox.base import Bind, ExecSpec, Sandbox
from migrationguard.sandbox.docker import DockerSandbox, docker_daemon_ok, image_exists
from migrationguard.sandbox.envs import ProvisionedEnv
from migrationguard.sandbox.local import LocalSandbox

RUNTIME_DIR = Path(__file__).parent / "runtime"


def make_sandbox(config: MigrationGuardConfig, log=lambda msg: None) -> Sandbox:
    backend = config.sandbox.backend
    image = config.image_for()
    docker = DockerSandbox(image, config.sandbox.memory, config.sandbox.cpus, config.sandbox.pids_limit, config.sandbox.network)
    if backend == "docker":
        ok, why = docker.check()
        if not ok:
            raise SandboxUnavailable(why)
        return docker
    if backend == "auto":
        ok, why = docker_daemon_ok()
        if ok and image_exists(image):
            return docker
        log(f"docker sandbox unavailable ({why if not ok else 'image ' + image + ' not built'}); using local process sandbox")
    return LocalSandbox(config.sandbox.memory, config.sandbox.timeout_seconds * 2, config.sandbox.network)


def detect_pythonpath(root: Path, configured: list[str]) -> list[str]:
    if configured:
        return configured
    paths = []
    if (root / "src").is_dir():
        paths.append("src")
    paths.append(".")
    return paths


@dataclass
class SandboxRunner:
    sandbox: Sandbox
    config: MigrationGuardConfig

    def _harness_dir(self, out_dir: Path) -> Path:
        harness = out_dir / "harness"
        if not harness.exists():
            shutil.copytree(RUNTIME_DIR, harness, ignore=shutil.ignore_patterns("__pycache__"))
        return harness

    def _pythonpath(self, workspace: Path) -> list[str]:
        return detect_pythonpath(workspace, self.config.environment.pythonpath)

    # ------------------------------------------------------------------ pytest
    def run_tests(self, env: ProvisionedEnv, workspace: Path, out_dir: Path, label: str,
                  extra_args: list[str] | None = None) -> TestRunResult:
        out_dir.mkdir(parents=True, exist_ok=True)
        harness = self._harness_dir(out_dir)
        results = out_dir / f"results-{label}"
        results.mkdir(exist_ok=True)
        report_name = "pytest-report.json"
        (results / report_name).unlink(missing_ok=True)
        pypath = ["${harness}"] + [f"${{workspace}}/{p}".rstrip("/.") or "${workspace}" for p in self._pythonpath(workspace)] + ["${site}"]
        spec = ExecSpec(
            argv=[
                "${python}", "-S", "-m", "pytest", "-p", "mg_pytest_plugin", "-p", "no:cacheprovider",
                "--rootdir", "${workspace}", "-o", "console_output_style=classic",
                *self.config.environment.pytest_args,
                *(extra_args or []),
            ],
            binds={
                "workspace": Bind(workspace, readonly=True),
                "site": Bind(env.site_dir, readonly=True),
                "harness": Bind(harness, readonly=True),
                "out": Bind(results, readonly=False),
            },
            env={
                "PYTHONPATH": ":".join(pypath),
                "MG_REPORT": f"${{out}}/{report_name}",
                "MG_TRACK_PACKAGES": ",".join(self.config.environment.track_packages),
            },
            timeout=self.config.sandbox.timeout_seconds,
            label=f"pytest-{label}",
            interpreter=env.interpreter,
        )
        result = self.sandbox.run(spec)
        report_path = results / report_name
        run = TestRunResult(label=label, exit_code=result.exit_code, duration_seconds=result.duration,
                            stdout_tail=result.tail(6000), timed_out=result.timed_out)
        if not report_path.exists():
            run.collection_errors.append(
                f"pytest produced no report (exit {result.exit_code}{', timed out' if result.timed_out else ''}): "
                + result.tail(3000)
            )
            return run
        data = json.loads(report_path.read_text(encoding="utf-8"))
        run.collected = list(data.get("collected", []))
        markers = data.get("markers", {})
        for nodeid, rec in data.get("tests", {}).items():
            run.tests[nodeid] = TestCaseResult(
                nodeid=nodeid,
                outcome=rec.get("outcome") or "error",
                duration=float(rec.get("duration") or 0.0),
                markers=list(markers.get(nodeid, [])),
                message=rec.get("message", ""),
            )
        for nodeid in run.collected:  # deselected / never-run tests still count as collected
            if nodeid not in run.tests:
                run.tests[nodeid] = TestCaseResult(nodeid=nodeid, outcome="skipped", markers=list(markers.get(nodeid, [])),
                                                   message="not executed")
        run.collection_errors.extend(data.get("collection_errors", []))
        run.warnings = [RecordedWarning(**w) for w in data.get("warnings", [])]
        run.packages = {k: v for k, v in (data.get("packages") or {}).items() if v}
        return run

    # ------------------------------------------------------------------ probes
    def run_probes(self, env: ProvisionedEnv, workspace: Path, out_dir: Path, label: str,
                   inputs_file: Path, discover: bool) -> ProbeRunResult:
        out_dir.mkdir(parents=True, exist_ok=True)
        harness = self._harness_dir(out_dir)
        results = out_dir / f"probes-{label}"
        results.mkdir(exist_ok=True)
        if not discover:
            shutil.copy2(inputs_file, results / "inputs.json")
        paths = self._pythonpath(workspace)
        spec = ExecSpec(
            argv=[
                "${python}", "-S", "${harness}/mg_probe_runner.py", "discover" if discover else "run",
                "--root", "${workspace}", "--paths", ":".join(paths),
                "--inputs", "${out}/inputs.json", "--out", "${out}/probe-results.json",
                "--limit", str(self.config.verify.max_probe_inputs_per_model),
            ],
            binds={
                "workspace": Bind(workspace, readonly=True),
                "site": Bind(env.site_dir, readonly=True),
                "harness": Bind(harness, readonly=True),
                "out": Bind(results, readonly=False),
            },
            env={"PYTHONPATH": "${site}"},
            timeout=self.config.sandbox.timeout_seconds,
            label=f"probes-{label}",
            interpreter=env.interpreter,
        )
        result = self.sandbox.run(spec)
        out_file = results / "probe-results.json"
        if result.exit_code != 0 or not out_file.exists():
            raise SandboxError(f"probe runner failed ({label}, exit {result.exit_code}): {result.tail(2500)}")
        if discover:
            shutil.copy2(results / "inputs.json", inputs_file)
        data = json.loads(out_file.read_text(encoding="utf-8"))
        return ProbeRunResult(
            label=label,
            pydantic_version=data.get("pydantic_version", ""),
            outcomes={pid: ProbeOutcome(probe_id=pid, **o) for pid, o in data.get("outcomes", {}).items()},
            import_errors=data.get("import_errors", []),
            import_warnings=[RecordedWarning(**w) for w in data.get("import_warnings", [])],
        )
