"""Permission policy for agent file access.

Every agent file operation goes through :meth:`PermissionPolicy.check_read` /
:meth:`check_write`. The policy is deliberately strict:

* paths are resolved (symlinks included) and must stay inside the worktree;
* ``.git`` and anything outside the writable globs is read-only;
* protected paths (tests, test configuration, CI, MigrationGuard config, user probes)
  can never be written or deleted by an agent;
* files that would alter interpreter or test-runner start-up (``conftest.py``,
  ``sitecustomize.py``, ``usercustomize.py``, ``*.pth``, ``pytest.ini`` ...) can never be
  created, wherever they live;
* writes are size-limited and must be valid UTF-8 text.

Violations raise :class:`PolicyViolation`, are counted, and are reported as evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from migrationguard.analysis.project import matches_any
from migrationguard.config import PolicySection
from migrationguard.errors import PolicyViolation

STARTUP_HOOK_NAMES = {
    "conftest.py",
    "sitecustomize.py",
    "usercustomize.py",
    "pytest.ini",
    ".pytest.ini",
    "tox.ini",
    "setup.cfg",
    "pyproject.toml.bak",
    "noxfile.py",
}


@dataclass
class PermissionPolicy:
    root: Path
    section: PolicySection
    denied: list[str] = field(default_factory=list)

    def _rel(self, path: str) -> str:
        if not path or path.startswith("~"):
            raise PolicyViolation(f"invalid path {path!r}")
        candidate = (self.root / path).resolve()
        root = self.root.resolve()
        try:
            rel = candidate.relative_to(root)
        except ValueError:
            raise PolicyViolation(f"path escapes the worktree: {path}") from None
        rel_posix = PurePosixPath(rel.as_posix()).as_posix()
        if rel_posix == "." or rel_posix.split("/")[0] == ".git":
            raise PolicyViolation(f"access to {path} is not allowed")
        return rel_posix

    def resolve_read(self, path: str) -> Path:
        rel = self._rel(path)
        return self.root / rel

    def check_write(self, path: str, content: str | None = None) -> Path:
        try:
            rel = self._rel(path)
            name = rel.rsplit("/", 1)[-1]
            target = self.root / rel
            if matches_any(rel, self.section.protected):
                raise PolicyViolation(f"{rel} is protected (tests, test configuration and CI cannot be modified by agents)")
            if name in STARTUP_HOOK_NAMES or name.endswith(".pth"):
                raise PolicyViolation(f"{rel} would change interpreter/test start-up and cannot be written")
            if not matches_any(rel, self.section.writable):
                raise PolicyViolation(f"{rel} is not in the writable set {self.section.writable}")
            if target.is_symlink():
                raise PolicyViolation(f"{rel} is a symlink")
            if content is not None:
                if len(content.encode("utf-8")) > self.section.max_write_bytes:
                    raise PolicyViolation(f"{rel}: write exceeds {self.section.max_write_bytes} bytes")
                if "\x00" in content:
                    raise PolicyViolation(f"{rel}: binary content is not allowed")
            return target
        except PolicyViolation as exc:
            self.denied.append(str(exc))
            raise

    def check_delete(self, path: str) -> Path:
        target = self.check_write(path)
        if not target.exists():
            raise PolicyViolation(f"{path} does not exist")
        return target
