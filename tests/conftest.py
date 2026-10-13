from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from migrationguard.analysis.project import ProjectIndex
from migrationguard.migrate.pydantic_codemod import migrate_source

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "benchmarks" / "cases"
FIXTURES = Path(__file__).parent / "fixtures"


def write_tree(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
    return root


def migrate(tmp_path: Path, files: dict[str, str], target: str):
    """Run the codemod over a small project and return (new_source, report) for ``target``."""
    write_tree(tmp_path, files)
    index = ProjectIndex.build(tmp_path)
    source = (tmp_path / target).read_text(encoding="utf-8")
    return migrate_source(source, target, index)


@pytest.fixture
def mg_home(tmp_path_factory, monkeypatch) -> Path:
    """Isolated MigrationGuard home that shares the (expensive) environment cache."""
    home = tmp_path_factory.mktemp("mghome")
    shared = Path(os.environ.get("MG_TEST_ENV_CACHE", Path.home() / ".cache" / "migrationguard-test-envs"))
    shared.mkdir(parents=True, exist_ok=True)
    (home / "envs").symlink_to(shared, target_is_directory=True)
    monkeypatch.setenv("MIGRATIONGUARD_HOME", str(home))
    return home


def make_repo(tmp_path: Path, case: str) -> Path:
    repo = tmp_path / f"repo-{case}"
    shutil.copytree(CASES / case / "repo", repo, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", "init"]):
        subprocess.run(["git", *args], cwd=repo, check=True, env=env, capture_output=True)
    return repo


def uv_available() -> bool:
    try:
        from migrationguard.sandbox.envs import uv_binary

        uv_binary()
        return True
    except Exception:
        return False
