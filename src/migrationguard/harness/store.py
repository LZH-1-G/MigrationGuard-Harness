"""SQLite persistence for runs, checkpoints, events, agent attempts, evidence and usage.

One database (``<home>/migrationguard.db``) holds every run. WAL mode keeps reads
(``migrationguard status``/``logs``) safe while a run is writing.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from migrationguard.models import AgentResult, AgentUsage, GateResult, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    branch TEXT,
    base_commit TEXT,
    phase TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    run_dir TEXT NOT NULL,
    decision_json TEXT
);
CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id),
    seq INTEGER NOT NULL,
    phase TEXT NOT NULL,
    commit_sha TEXT,
    state_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS checkpoints_run ON checkpoints(run_id, seq);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    phase TEXT,
    kind TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    data_json TEXT
);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id, id);
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    iteration INTEGER NOT NULL,
    role TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    commit_sha TEXT,
    summary TEXT,
    claims_json TEXT,
    usage_json TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    iteration INTEGER NOT NULL,
    gate TEXT NOT NULL,
    status TEXT NOT NULL,
    blocking INTEGER NOT NULL,
    summary TEXT NOT NULL,
    details_json TEXT,
    artifacts_json TEXT,
    commit_sha TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS evidence_run ON evidence(run_id, iteration);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    phase TEXT,
    role TEXT,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_creation_tokens INTEGER,
    cost_usd REAL,
    turns INTEGER,
    ts TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    @contextmanager
    def _tx(self):
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ runs
    def create_run(self, run_id: str, repo: str, branch: str, base_commit: str, run_dir: str, phase: str) -> None:
        now = utcnow().isoformat()
        with self._tx() as c:
            c.execute(
                "INSERT INTO runs(id, repo, branch, base_commit, phase, created_at, updated_at, run_dir) VALUES (?,?,?,?,?,?,?,?)",
                (run_id, repo, branch, base_commit, phase, now, now, run_dir),
            )

    def update_run(self, run_id: str, phase: str, decision: dict | None = None) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE runs SET phase=?, updated_at=?, decision_json=COALESCE(?, decision_json) WHERE id=?",
                (phase, utcnow().isoformat(), json.dumps(decision) if decision else None, run_id),
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE id=? OR id LIKE ?", (run_id, f"{run_id}%")).fetchone()
        return dict(row) if row else None

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ checkpoints
    def checkpoint(self, run_id: str, phase: str, commit_sha: str | None, state_json: str) -> int:
        with self._tx() as c:
            seq = c.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM checkpoints WHERE run_id=?", (run_id,)).fetchone()[0]
            c.execute(
                "INSERT INTO checkpoints(run_id, seq, phase, commit_sha, state_json, created_at) VALUES (?,?,?,?,?,?)",
                (run_id, seq, phase, commit_sha, state_json, utcnow().isoformat()),
            )
            c.execute("UPDATE runs SET phase=?, updated_at=? WHERE id=?", (phase, utcnow().isoformat(), run_id))
        return int(seq)

    def latest_checkpoint(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM checkpoints WHERE run_id=? ORDER BY seq DESC LIMIT 1", (run_id,)
            ).fetchone()
        return dict(row) if row else None

    def checkpoints(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, phase, commit_sha, created_at FROM checkpoints WHERE run_id=? ORDER BY seq", (run_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ events
    def add_event(self, run_id: str, phase: str | None, kind: str, level: str, message: str, data: dict | None) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO events(run_id, ts, phase, kind, level, message, data_json) VALUES (?,?,?,?,?,?,?)",
                (run_id, utcnow().isoformat(), phase, kind, level, message, json.dumps(data, default=str) if data else None),
            )

    def events(self, run_id: str, after_id: int = 0, limit: int = 10_000) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id LIMIT ?", (run_id, after_id, limit)
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ attempts
    def start_attempt(self, run_id: str, iteration: int, role: str) -> int:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO attempts(run_id, iteration, role, status, started_at) VALUES (?,?,?,?,?)",
                (run_id, iteration, role, "running", utcnow().isoformat()),
            )
            return int(cur.lastrowid)

    def finish_attempt(self, attempt_id: int, result: AgentResult | None, status: str, commit_sha: str | None, error: str = "") -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE attempts SET status=?, ended_at=?, commit_sha=?, summary=?, claims_json=?, usage_json=?, error=? WHERE id=?",
                (
                    status,
                    utcnow().isoformat(),
                    commit_sha,
                    result.summary if result else "",
                    json.dumps([cl.model_dump() for cl in result.claims]) if result else "[]",
                    result.usage.model_dump_json() if result else None,
                    error,
                    attempt_id,
                ),
            )

    def mark_interrupted_attempts(self, run_id: str) -> int:
        with self._tx() as c:
            cur = c.execute(
                "UPDATE attempts SET status='interrupted', ended_at=? WHERE run_id=? AND status='running'",
                (utcnow().isoformat(), run_id),
            )
            return cur.rowcount

    def attempts(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM attempts WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ evidence & usage
    def add_evidence(self, run_id: str, iteration: int, commit_sha: str, gates: list[GateResult]) -> None:
        now = utcnow().isoformat()
        with self._tx() as c:
            c.executemany(
                "INSERT INTO evidence(run_id, iteration, gate, status, blocking, summary, details_json, artifacts_json, commit_sha, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    (run_id, iteration, g.gate, g.status, int(g.blocking), g.summary, json.dumps(g.details, default=str),
                     json.dumps(g.artifacts), commit_sha, now)
                    for g in gates
                ],
            )

    def evidence(self, run_id: str, iteration: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if iteration is None:
                rows = self._conn.execute("SELECT * FROM evidence WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM evidence WHERE run_id=? AND iteration=? ORDER BY id", (run_id, iteration)
                ).fetchall()
        return [dict(r) for r in rows]

    def add_usage(self, run_id: str, phase: str, role: str, model: str | None, usage: AgentUsage) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO usage(run_id, phase, role, model, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, cost_usd, turns, ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, phase, role, model, usage.input_tokens, usage.output_tokens, usage.cache_read_tokens,
                 usage.cache_creation_tokens, usage.cost_usd, usage.turns, utcnow().isoformat()),
            )

    def usage_total(self, run_id: str) -> AgentUsage:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), COALESCE(SUM(cache_read_tokens),0), "
                "COALESCE(SUM(cache_creation_tokens),0), COALESCE(SUM(cost_usd),0), COALESCE(SUM(turns),0) FROM usage WHERE run_id=?",
                (run_id,),
            ).fetchone()
        return AgentUsage(input_tokens=row[0], output_tokens=row[1], cache_read_tokens=row[2], cache_creation_tokens=row[3],
                          cost_usd=row[4], turns=row[5])
