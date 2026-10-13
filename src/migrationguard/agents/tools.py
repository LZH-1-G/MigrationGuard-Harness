"""The agent toolbox: the only way an agent can observe or change anything.

Read tools are confined to the worktree; write tools go through the
:class:`~migrationguard.sandbox.policy.PermissionPolicy`; execution tools run inside the
sandbox via callbacks into the harness. Every call is counted and logged; denied calls
are returned to the agent as an explanation (so it can adapt) and recorded as evidence.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from migrationguard.analysis.project import iter_python_files
from migrationguard.errors import PolicyViolation
from migrationguard.models import AgentClaim, ReviewIssue, ReviewVerdict
from migrationguard.sandbox.policy import PermissionPolicy

MAX_READ_CHARS = 40_000
MAX_SEARCH_HITS = 120


@dataclass
class ToolRuntime:
    """Harness callbacks (sandboxed) exposed to agents. All return human-readable text."""

    run_tests: Callable[[list[str]], str]
    run_probes: Callable[[], str]
    findings: Callable[[], str]
    diff: Callable[[], str]
    evidence: Callable[[], str]


@dataclass
class AgentToolbox:
    root: Path
    policy: PermissionPolicy
    runtime: ToolRuntime
    max_test_runs: int = 12
    log: Callable[[str, dict[str, Any]], None] = lambda name, data: None
    read_only: bool = False
    calls: int = 0
    denied: int = 0
    test_runs: int = 0
    files_written: set[str] = field(default_factory=set)
    finish_summary: str | None = None
    claims: list[AgentClaim] = field(default_factory=list)
    verdict: ReviewVerdict | None = None

    # ------------------------------------------------------------------ plumbing
    def _call(self, name: str, args: dict[str, Any], fn: Callable[[], str]) -> str:
        self.calls += 1
        try:
            result = fn()
            self.log(name, {"args": _short(args), "ok": True})
            return result
        except PolicyViolation as exc:
            self.denied += 1
            self.log(name, {"args": _short(args), "denied": str(exc)})
            return f"DENIED by MigrationGuard policy: {exc}"
        except Exception as exc:  # tool errors are returned to the agent, not raised
            self.log(name, {"args": _short(args), "error": str(exc)[:300]})
            return f"ERROR: {type(exc).__name__}: {exc}"

    # ------------------------------------------------------------------ read tools
    def list_files(self, pattern: str = "**/*.py") -> str:
        def run() -> str:
            files = [p.relative_to(self.root).as_posix() for p in iter_python_files(self.root)]
            for extra in ("requirements*.txt", "pyproject.toml", "setup.cfg", "setup.py"):
                files.extend(p.relative_to(self.root).as_posix() for p in self.root.glob(extra))
            matched = sorted({f for f in files if fnmatch.fnmatch(f, pattern) or fnmatch.fnmatch(f, pattern.lstrip("*/"))})
            return "\n".join(matched) or "(no files)"

        return self._call("list_files", {"pattern": pattern}, run)

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        def run() -> str:
            target = self.policy.resolve_read(path)
            if not target.is_file():
                return f"ERROR: {path} does not exist"
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
            start = max(1, int(start_line or 1))
            end = min(len(lines), int(end_line) if end_line else len(lines))
            out = [f"{i:5d}| {lines[i - 1]}" for i in range(start, end + 1)]
            text = "\n".join(out)
            if len(text) > MAX_READ_CHARS:
                text = text[:MAX_READ_CHARS] + f"\n... truncated; read a narrower range (file has {len(lines)} lines)"
            return f"{path} (lines {start}-{end} of {len(lines)})\n{text}"

        return self._call("read_file", {"path": path, "start": start_line, "end": end_line}, run)

    def search(self, pattern: str, glob: str = "**/*.py") -> str:
        def run() -> str:
            regex = re.compile(pattern)
            hits = []
            for p in iter_python_files(self.root):
                rel = p.relative_to(self.root).as_posix()
                if not (fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(rel, glob.lstrip("*/"))):
                    continue
                for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if regex.search(line):
                        hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                        if len(hits) >= MAX_SEARCH_HITS:
                            return "\n".join(hits) + "\n... (more matches truncated)"
            return "\n".join(hits) or "(no matches)"

        return self._call("search", {"pattern": pattern, "glob": glob}, run)

    # ------------------------------------------------------------------ write tools
    def write_file(self, path: str, content: str) -> str:
        def run() -> str:
            if self.read_only:
                raise PolicyViolation("this agent role is read-only")
            target = self.policy.check_write(path, content)
            if path.endswith(".py"):
                try:
                    compile(content, path, "exec", dont_inherit=True)
                except SyntaxError as exc:
                    return f"REJECTED: content is not valid Python ({exc}); file left unchanged"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            self.files_written.add(path)
            return f"wrote {path} ({len(content.splitlines())} lines)"

        return self._call("write_file", {"path": path, "bytes": len(content or "")}, run)

    def replace_in_file(self, path: str, old: str, new: str) -> str:
        def run() -> str:
            if self.read_only:
                raise PolicyViolation("this agent role is read-only")
            target = self.policy.check_write(path)
            if not target.is_file():
                return f"ERROR: {path} does not exist"
            text = target.read_text(encoding="utf-8")
            count = text.count(old)
            if count == 0:
                return "ERROR: old text not found (it must match exactly, including indentation)"
            if count > 1:
                return f"ERROR: old text occurs {count} times; include more context to make it unique"
            updated = text.replace(old, new, 1)
            self.policy.check_write(path, updated)
            if path.endswith(".py"):
                try:
                    compile(updated, path, "exec", dont_inherit=True)
                except SyntaxError as exc:
                    return f"REJECTED: result would not be valid Python ({exc}); file left unchanged"
            target.write_text(updated, encoding="utf-8")
            self.files_written.add(path)
            return f"edited {path}"

        return self._call("replace_in_file", {"path": path, "old": old[:80]}, run)

    # ------------------------------------------------------------------ sandboxed execution
    def run_tests(self, node_ids: list[str] | None = None) -> str:
        def run() -> str:
            if self.test_runs >= self.max_test_runs:
                return f"LIMIT: test-run budget for this attempt ({self.max_test_runs}) exhausted; call finish"
            self.test_runs += 1
            return self.runtime.run_tests(list(node_ids or []))

        return self._call("run_tests", {"node_ids": node_ids}, run)

    def run_probes(self) -> str:
        def run() -> str:
            if self.test_runs >= self.max_test_runs:
                return "LIMIT: execution budget for this attempt exhausted; call finish"
            self.test_runs += 1
            return self.runtime.run_probes()

        return self._call("run_probes", {}, run)

    def show_findings(self) -> str:
        return self._call("show_findings", {}, self.runtime.findings)

    def show_diff(self) -> str:
        return self._call("show_diff", {}, self.runtime.diff)

    def show_evidence(self) -> str:
        return self._call("show_evidence", {}, self.runtime.evidence)

    # ------------------------------------------------------------------ completion
    def finish(self, summary: str, claims: list[str] | None = None) -> str:
        def run() -> str:
            self.finish_summary = summary
            self.claims = [AgentClaim(text=c) for c in (claims or [])]
            return "Recorded. MigrationGuard will now verify your changes independently; claims are not trusted."

        return self._call("finish", {"claims": len(claims or [])}, run)

    def submit_review(self, verdict: str, summary: str, issues: list[dict[str, Any]] | None = None) -> str:
        def run() -> str:
            parsed = []
            for issue in issues or []:
                if isinstance(issue, dict):
                    parsed.append(ReviewIssue(
                        path=str(issue.get("path", "")),
                        line=int(issue.get("line", 0) or 0),
                        severity=issue.get("severity", "major") if issue.get("severity") in ("blocker", "major", "minor") else "major",
                        explanation=str(issue.get("explanation", ""))[:2000],
                    ))
            self.verdict = ReviewVerdict(verdict="approve" if verdict == "approve" else "reject", summary=summary[:4000], issues=parsed)
            return "Review recorded."

        return self._call("submit_review", {"verdict": verdict, "issues": len(issues or [])}, run)


def _short(args: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in args.items():
        s = repr(v)
        out[k] = s if len(s) <= 120 else s[:117] + "..."
    return out
