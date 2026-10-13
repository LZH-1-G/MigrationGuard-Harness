from __future__ import annotations

import os

import pytest

from migrationguard.agents.tools import AgentToolbox, ToolRuntime
from migrationguard.config import PolicySection
from migrationguard.errors import PolicyViolation
from migrationguard.sandbox.policy import PermissionPolicy
from tests.conftest import write_tree


@pytest.fixture
def repo(tmp_path):
    write_tree(tmp_path, {
        "app/models.py": "x = 1\n",
        "tests/test_app.py": "def test_x():\n    assert True\n",
        "requirements.txt": "pydantic<2\n",
        "migrationguard.toml": "",
    })
    (tmp_path / ".git").mkdir()
    return tmp_path


def test_policy_allows_source_and_manifests(repo):
    policy = PermissionPolicy(repo, PolicySection())
    assert policy.check_write("app/models.py", "x = 2\n") == repo / "app/models.py"
    policy.check_write("requirements.txt", "pydantic>=2\n")
    policy.check_write("app/new_module.py", "")


@pytest.mark.parametrize("path, why", [
    ("tests/test_app.py", "protected"),
    ("app/test_helpers.py", "protected"),
    ("conftest.py", "protected"),
    ("app/conftest.py", "protected"),
    ("sitecustomize.py", "start-up"),
    ("app/evil.pth", "start-up"),
    ("pytest.ini", "protected"),
    ("migrationguard.toml", "protected"),
    (".git/config", "not allowed"),
    ("../outside.py", "escapes"),
    ("/etc/passwd", "escapes"),
    ("README.md", "writable set"),
])
def test_policy_denies(repo, path, why):
    policy = PermissionPolicy(repo, PolicySection())
    with pytest.raises(PolicyViolation, match=why):
        policy.check_write(path, "x")
    assert policy.denied


def test_policy_denies_symlink_escape(repo, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    os.symlink(outside, repo / "app" / "link")
    with pytest.raises(PolicyViolation, match="escapes"):
        PermissionPolicy(repo, PolicySection()).check_write("app/link/x.py", "x")


def test_policy_size_limit(repo):
    policy = PermissionPolicy(repo, PolicySection(max_write_bytes=10))
    with pytest.raises(PolicyViolation, match="exceeds"):
        policy.check_write("app/models.py", "x" * 11)


def _toolbox(repo, **kw):
    runtime = ToolRuntime(run_tests=lambda ids: "ok", run_probes=lambda: "ok", findings=lambda: "none",
                          diff=lambda: "", evidence=lambda: "[]")
    return AgentToolbox(root=repo, policy=PermissionPolicy(repo, PolicySection()), runtime=runtime, **kw)


def test_toolbox_denials_are_reported_not_raised(repo):
    box = _toolbox(repo)
    result = box.write_file("tests/test_app.py", "def test_x():\n    pass\n")
    assert result.startswith("DENIED")
    assert "assert True" in (repo / "tests/test_app.py").read_text()
    assert box.denied == 1 and box.calls == 1


def test_toolbox_rejects_invalid_python(repo):
    box = _toolbox(repo)
    assert box.write_file("app/models.py", "def broken(:\n").startswith("REJECTED")
    assert (repo / "app/models.py").read_text() == "x = 1\n"


def test_toolbox_replace_requires_unique_match(repo):
    box = _toolbox(repo)
    (repo / "app/models.py").write_text("a = 1\na = 1\n")
    assert "occurs 2 times" in box.replace_in_file("app/models.py", "a = 1", "a = 2")
    assert box.replace_in_file("app/models.py", "a = 1\na = 1", "a = 3") == "edited app/models.py"
    assert box.files_written == {"app/models.py"}


def test_read_only_toolbox(repo):
    box = _toolbox(repo, read_only=True)
    assert box.write_file("app/models.py", "x = 3\n").startswith("DENIED")
    assert "x = 1" in box.read_file("app/models.py")


def test_read_file_confined_to_worktree(repo):
    box = _toolbox(repo)
    assert box.read_file("../../etc/passwd").startswith("DENIED")


def test_test_run_budget(repo):
    box = _toolbox(repo, max_test_runs=2)
    assert box.run_tests() == "ok"
    assert box.run_probes() == "ok"
    assert box.run_tests().startswith("LIMIT")


def test_finish_and_review_recorded(repo):
    box = _toolbox(repo)
    box.finish("done", ["All tests pass"])
    assert box.finish_summary == "done" and box.claims[0].text == "All tests pass"
    box.submit_review("reject", "bad", [{"path": "a.py", "line": 3, "severity": "blocker", "explanation": "x"}])
    assert box.verdict.verdict == "reject" and box.verdict.issues[0].severity == "blocker"
