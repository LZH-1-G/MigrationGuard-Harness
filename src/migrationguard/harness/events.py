"""Structured event log: SQLite + JSONL file + optional live sink (the CLI console)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from migrationguard.harness.store import Store
from migrationguard.models import utcnow

EventSink = Callable[[dict[str, Any]], None]


class EventLog:
    def __init__(self, store: Store, run_id: str, jsonl: Path, sink: EventSink | None = None):
        self.store = store
        self.run_id = run_id
        self.jsonl = jsonl
        self.sink = sink
        self.phase: str | None = None
        jsonl.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, kind: str, message: str, level: str = "info", **data: Any) -> None:
        event = {
            "ts": utcnow().isoformat(),
            "run_id": self.run_id,
            "phase": self.phase,
            "kind": kind,
            "level": level,
            "message": message,
            "data": data or None,
        }
        self.store.add_event(self.run_id, self.phase, kind, level, message, data or None)
        with self.jsonl.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, default=str) + "\n")
        if self.sink:
            try:
                self.sink(event)
            except Exception:  # a broken console must never break a run
                pass

    def info(self, message: str, **data: Any) -> None:
        self.emit("log", message, "info", **data)

    def warn(self, message: str, **data: Any) -> None:
        self.emit("log", message, "warning", **data)

    def error(self, message: str, **data: Any) -> None:
        self.emit("log", message, "error", **data)
