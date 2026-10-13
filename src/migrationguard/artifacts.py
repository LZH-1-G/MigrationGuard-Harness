"""Content-addressed evidence artifacts for a run.

Every piece of evidence a gate relies on (test reports, probe results, diffs, locks) is
written under ``<run_dir>/artifacts`` and referenced by SHA-256 from the evidence table,
so a report can be re-checked after the fact.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, name: str) -> Path:
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def save_text(self, name: str, text: str) -> tuple[Path, str]:
        p = self.path(name)
        p.write_text(text, encoding="utf-8")
        return p, hashlib.sha256(text.encode("utf-8")).hexdigest()

    def save_json(self, name: str, obj: Any) -> tuple[Path, str]:
        if isinstance(obj, BaseModel):
            text = obj.model_dump_json(indent=2)
        else:
            text = json.dumps(obj, indent=2, sort_keys=True, default=_default)
        return self.save_text(name, text)

    def load_json(self, name: str) -> Any:
        return json.loads((self.root / name).read_text(encoding="utf-8"))

    def exists(self, name: str) -> bool:
        return (self.root / name).exists()


def _default(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, set):
        return sorted(obj)
    return str(obj)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
