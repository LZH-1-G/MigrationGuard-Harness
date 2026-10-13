from __future__ import annotations

import json
import shutil

from typer.testing import CliRunner

from migrationguard.cli import app
from tests.conftest import CASES

runner = CliRunner()


def test_help_and_version():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0 and "run" in result.output and "resume" in result.output
    assert runner.invoke(app, ["--version"]).output.startswith("migrationguard ")


def test_analyze_json(tmp_path):
    repo = tmp_path / "billing"
    shutil.copytree(CASES / "billing" / "repo", repo)
    result = runner.invoke(app, ["analyze", str(repo), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["summary"]["models"] == 2
    assert data["summary"]["unresolved"] == 0
    assert any(step["kind"] == "dependency" for step in data["plan"]["steps"])


def test_analyze_table_lists_agent_work(tmp_path):
    repo = tmp_path / "catalog"
    shutil.copytree(CASES / "catalog" / "repo", repo)
    result = runner.invoke(app, ["analyze", str(repo)])
    assert result.exit_code == 0
    assert "PYD204" in result.output and "PYD202" in result.output


def test_init_writes_config(tmp_path):
    assert runner.invoke(app, ["init", str(tmp_path)]).exit_code == 0
    assert "[verify]" in (tmp_path / "migrationguard.toml").read_text()
    assert runner.invoke(app, ["init", str(tmp_path)]).exit_code == 1


def test_bench_list():
    result = runner.invoke(app, ["bench", "list"])
    assert result.exit_code == 0 and "billing" in result.output and "migrationguard" in result.output


def test_run_requires_git_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("MIGRATIONGUARD_HOME", str(tmp_path / "home"))
    (tmp_path / "proj").mkdir()
    result = runner.invoke(app, ["run", str(tmp_path / "proj"), "--agent", "none"])
    assert result.exit_code == 2 and "not a git repository" in " ".join(result.output.split())
