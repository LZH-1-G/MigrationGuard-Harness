"""Git worktree management.

MigrationGuard never edits the user's checkout. Every run gets:

* a **target worktree** on branch ``migrationguard/<run-id>`` where codemods and agents
  work; every phase / repair attempt is committed so a run can be resumed from (and
  audited at) any checkpoint;
* a **baseline worktree** detached at the base commit, used to build the oracle
  (baseline test results and behavioural probes) and to audit changes.

Commits are made with a fixed identity, hooks disabled and signing off, so user git
configuration can neither block nor execute code during a run.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from migrationguard.errors import RepositoryError

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "MigrationGuard",
    "GIT_AUTHOR_EMAIL": "migrationguard@localhost",
    "GIT_COMMITTER_NAME": "MigrationGuard",
    "GIT_COMMITTER_EMAIL": "migrationguard@localhost",
    "GIT_TERMINAL_PROMPT": "0",
}
_GIT_FLAGS = ["-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false"]
_EXCLUDES = [":(exclude,glob)**/__pycache__/**", ":(exclude,glob)**/*.pyc", ":(exclude,glob)**/.pytest_cache/**"]


def git(cwd: Path, *args: str, check: bool = True, input_text: str | None = None) -> str:
    env = {**os.environ, **_GIT_ENV}
    proc = subprocess.run(
        ["git", *_GIT_FLAGS, *args],
        cwd=cwd,
        env=env,
        input=input_text,
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        raise RepositoryError(f"git {' '.join(args)} failed in {cwd}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


@dataclass
class ChangedFile:
    status: str  # A, M, D, R...
    path: str
    old_path: str | None = None


class GitRepo:
    def __init__(self, root: Path):
        self.root = root

    @classmethod
    def open(cls, path: Path) -> GitRepo:
        path = path.resolve()
        if not path.exists():
            raise RepositoryError(f"{path} does not exist")
        try:
            top = git(path, "rev-parse", "--show-toplevel").strip()
        except RepositoryError as exc:
            raise RepositoryError(
                f"{path} is not a git repository. MigrationGuard works on git repositories so that "
                "every change is isolated and reviewable (run `git init && git add -A && git commit -m init`)."
            ) from exc
        return cls(Path(top))

    @classmethod
    def init(cls, path: Path, message: str = "initial commit") -> GitRepo:
        git(path, "init", "-q", "-b", "main")
        git(path, "add", "-A", "--", ".", *_EXCLUDES)
        git(path, "commit", "-q", "--no-verify", "--allow-empty", "-m", message)
        return cls(path.resolve())

    def head(self) -> str:
        return git(self.root, "rev-parse", "HEAD").strip()

    def resolve(self, ref: str) -> str:
        return git(self.root, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()

    def is_dirty(self) -> bool:
        return bool(git(self.root, "status", "--porcelain", "--untracked-files=no").strip())

    def snapshot(self, include_uncommitted: bool) -> str:
        """Commit to migrate from. With ``include_uncommitted`` a dangling stash commit
        capturing tracked changes is used (the working tree is not modified)."""
        if include_uncommitted and self.is_dirty():
            sha = git(self.root, "stash", "create", "migrationguard snapshot").strip()
            if sha:
                return sha
        return self.head()

    def branch_exists(self, name: str) -> bool:
        return git(self.root, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}", check=False).strip() != ""


class Worktree:
    """A git worktree owned by MigrationGuard."""

    def __init__(self, repo: GitRepo, path: Path):
        self.repo = repo
        self.path = path

    def head(self) -> str:
        return git(self.path, "rev-parse", "HEAD").strip()

    def commit_all(self, message: str) -> str:
        git(self.path, "add", "-A", "--", ".", *_EXCLUDES)
        if self._staged_empty():
            return self.head()
        git(self.path, "commit", "-q", "--no-verify", "-m", message)
        return self.head()

    def _staged_empty(self) -> bool:
        proc = subprocess.run(
            ["git", *_GIT_FLAGS, "diff", "--cached", "--quiet"], cwd=self.path, capture_output=True
        )
        return proc.returncode == 0

    def reset_hard(self, sha: str) -> None:
        git(self.path, "reset", "-q", "--hard", sha)
        git(self.path, "clean", "-q", "-fdx", "-e", ".mg-keep")

    def is_clean(self) -> bool:
        return git(self.path, "status", "--porcelain").strip() == ""

    def diff(self, base: str, paths: list[str] | None = None, stat: bool = False, unified: int = 3) -> str:
        args = ["diff", "--no-color", "--no-ext-diff", f"-U{unified}", "-M"]
        if stat:
            args.append("--stat")
        args.append(base)
        args.append("--")
        args.extend(paths or [])
        return git(self.path, *args)

    def working_diff(self) -> str:
        git(self.path, "add", "-A", "--", ".", *_EXCLUDES)
        return git(self.path, "diff", "--cached", "--no-color", "-M")

    def changed_files(self, base: str, head: str = "HEAD") -> list[ChangedFile]:
        out = git(self.path, "diff", "--name-status", "-M", base, head)
        files: list[ChangedFile] = []
        for line in out.splitlines():
            parts = line.split("\t")
            status = parts[0]
            if status.startswith("R") and len(parts) == 3:
                files.append(ChangedFile("R", parts[2], parts[1]))
            elif len(parts) >= 2:
                files.append(ChangedFile(status[0], parts[1]))
        return files

    def uncommitted_files(self) -> list[str]:
        out = git(self.path, "status", "--porcelain", "-uall")
        return [line[3:].split(" -> ")[-1] for line in out.splitlines() if line.strip()]

    def show(self, rev: str, path: str) -> str | None:
        proc = subprocess.run(
            ["git", *_GIT_FLAGS, "show", f"{rev}:{path}"], cwd=self.path, capture_output=True, text=True
        )
        return proc.stdout if proc.returncode == 0 else None


class WorktreeManager:
    def __init__(self, repo: GitRepo, run_dir: Path):
        self.repo = repo
        self.run_dir = run_dir

    @property
    def target_path(self) -> Path:
        return self.run_dir / "worktrees" / "target"

    @property
    def baseline_path(self) -> Path:
        return self.run_dir / "worktrees" / "baseline"

    def create(self, branch: str, base: str) -> tuple[Worktree, Worktree]:
        git(self.repo.root, "worktree", "prune")
        self.target_path.parent.mkdir(parents=True, exist_ok=True)
        if not (self.target_path / ".git").exists():
            if self.repo.branch_exists(branch):
                git(self.repo.root, "worktree", "add", "-q", str(self.target_path), branch)
            else:
                git(self.repo.root, "worktree", "add", "-q", "-b", branch, str(self.target_path), base)
        if not (self.baseline_path / ".git").exists():
            git(self.repo.root, "worktree", "add", "-q", "--detach", str(self.baseline_path), base)
        return Worktree(self.repo, self.target_path), Worktree(self.repo, self.baseline_path)

    def open(self) -> tuple[Worktree, Worktree]:
        if not (self.target_path / ".git").exists() or not (self.baseline_path / ".git").exists():
            raise RepositoryError(f"worktrees for this run are missing under {self.run_dir}")
        return Worktree(self.repo, self.target_path), Worktree(self.repo, self.baseline_path)

    def remove(self, keep_branch: bool = True, branch: str | None = None) -> None:
        for path in (self.target_path, self.baseline_path):
            if path.exists():
                git(self.repo.root, "worktree", "remove", "--force", str(path), check=False)
                shutil.rmtree(path, ignore_errors=True)
        git(self.repo.root, "worktree", "prune", check=False)
        if not keep_branch and branch:
            git(self.repo.root, "branch", "-D", branch, check=False)
