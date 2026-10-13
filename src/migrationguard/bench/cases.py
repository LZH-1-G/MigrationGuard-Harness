"""Benchmark case discovery.

A case is a directory with:

``case.toml``  name, difficulty, description, traps, expected outcome
``repo/``      a small but realistic Pydantic v1 project with its own visible tests
``oracle/``    a *hidden* behavioural test suite. It is never shown to agents and never used
               by the harness; the benchmark uses it only to establish ground truth.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


@dataclass
class BenchCase:
    name: str
    root: Path
    difficulty: str = "medium"
    description: str = ""
    traps: list[str] = field(default_factory=list)
    expected: str = ""

    @property
    def repo(self) -> Path:
        return self.root / "repo"

    @property
    def oracle(self) -> Path:
        return self.root / "oracle"


def benchmarks_dir() -> Path:
    env = os.environ.get("MIGRATIONGUARD_BENCHMARKS")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for candidate in (here.parents[3] / "benchmarks", here.parents[1] / "_benchmarks"):
        if (candidate / "cases").is_dir():
            return candidate
    raise FileNotFoundError("benchmark cases not found (set MIGRATIONGUARD_BENCHMARKS)")


def load_cases(names: list[str] | None = None, root: Path | None = None) -> list[BenchCase]:
    base = (root or benchmarks_dir()) / "cases"
    cases = []
    for case_dir in sorted(p for p in base.iterdir() if (p / "case.toml").is_file()):
        meta = tomllib.loads((case_dir / "case.toml").read_text(encoding="utf-8"))
        cases.append(BenchCase(
            name=meta.get("name", case_dir.name),
            root=case_dir,
            difficulty=meta.get("difficulty", "medium"),
            description=meta.get("description", ""),
            traps=list(meta.get("traps", [])),
            expected=meta.get("expected", ""),
        ))
    if names:
        known = {c.name for c in cases}
        missing = [n for n in names if n not in known]
        if missing:
            raise ValueError(f"unknown case(s) {missing}; available: {sorted(known)}")
        cases = [c for c in cases if c.name in names]
    return cases
