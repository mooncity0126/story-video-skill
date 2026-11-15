from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path

from .util import BudgetError, canonical, now


class Store:
    """SQLite transactions protect budget reservations and crash recovery boundaries."""

    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "state.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.db:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS nodes (
                  node_id TEXT PRIMARY KEY, recipe TEXT NOT NULL, current_id TEXT,
                  revision INTEGER NOT NULL DEFAULT 0, previous_id TEXT);
                CREATE TABLE IF NOT EXISTS attempts (
                  attempt_id TEXT PRIMARY KEY, node_id TEXT NOT NULL, input_hash TEXT NOT NULL,
                  status TEXT NOT NULL, remote_status TEXT, task_id TEXT, quote REAL,
                  reserved REAL NOT NULL DEFAULT 0, request TEXT, output TEXT,
                  error TEXT, parent_id TEXT, created_at TEXT, updated_at TEXT);
                CREATE TABLE IF NOT EXISTS events (
                  event_id INTEGER PRIMARY KEY, timestamp TEXT, node_id TEXT, event TEXT, detail TEXT);
            """)

    def close(self):
        self.db.close()

    def sync_nodes(self, nodes):
        with self.lock, self.db:
            existing = {r["node_id"] for r in self.db.execute("SELECT node_id FROM nodes")}
            removed = existing - set(nodes)
            for node_id in removed:
                self.db.execute("DELETE FROM nodes WHERE node_id=?", (node_id,))
            for node_id, recipe in nodes.items():
                self.db.execute("INSERT INTO nodes(node_id,recipe) VALUES(?,?) ON CONFLICT(node_id) DO UPDATE SET recipe=excluded.recipe", (node_id, canonical(recipe)))

    def nodes(self):
        with self.lock:
            rows = self.db.execute("SELECT * FROM nodes").fetchall()
            return {r["node_id"]: {**dict(r), "recipe": json.loads(r["recipe"])} for r in rows}

    def current(self, node_id):
        with self.lock:
            row = self.db.execute("SELECT a.* FROM attempts a JOIN nodes n ON a.attempt_id=n.current_id WHERE n.node_id=?", (node_id,)).fetchone()
            return self._decode(row)

    def revision(self, node_id):
        with self.lock:
            return self.db.execute("SELECT revision FROM nodes WHERE node_id=?", (node_id,)).fetchone()[0]

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        result = dict(row)
        for key in ("request", "output"):
            result[key] = json.loads(result[key]) if result[key] else None
        return result

    def attempt(self, node_id, input_hash, request, *, force=False):
        with self.lock, self.db:
            current = self.current(node_id)
            if force and current and current["status"] not in {"remote_failed", "rejected"}:
                raise ValueError("Only an explicitly failed task can create a fallback candidate")
            if current and current["input_hash"] == input_hash and not force:
                if current["status"] == "submitting" and not current["task_id"]:
                    self.update(current["attempt_id"], status="submission_unknown", error="进程在提交期间退出，必须核实云端历史")
                    return self.current(node_id)
                return current
            node = self.db.execute("SELECT * FROM nodes WHERE node_id=?", (node_id,)).fetchone()
            attempt_id = "TRY_" + uuid.uuid4().hex
            parent = current["attempt_id"] if current else node["previous_id"]
            self.db.execute(
                "INSERT INTO attempts(attempt_id,node_id,input_hash,status,request,parent_id,created_at,updated_at) VALUES(?,?,?,'planned',?,?,?,?)",
                (attempt_id, node_id, input_hash, canonical(request), parent, now(), now()),
            )
            self.db.execute("UPDATE nodes SET current_id=? WHERE node_id=?", (attempt_id, node_id))
            self.event(node_id, "candidate_created", attempt_id)
            return self.current(node_id)

    def update(self, attempt_id, **fields):
        allowed = {"status", "remote_status", "task_id", "quote", "reserved", "request", "output", "error"}
        if set(fields) - allowed:
            raise ValueError("unsupported state field")
        values = {k: canonical(v) if k in {"request", "output"} and v is not None else v for k, v in fields.items()}
        values["updated_at"] = now()
        with self.lock, self.db:
            self.db.execute("UPDATE attempts SET " + ",".join(k + "=?" for k in values) + " WHERE attempt_id=?", (*values.values(), attempt_id))

    def reserve(self, attempt_id, quote, budget):
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            spent = self.db.execute("SELECT COALESCE(SUM(reserved),0) FROM attempts WHERE attempt_id<>?", (attempt_id,)).fetchone()[0]
            if budget is None or spent + quote > budget:
                raise BudgetError(f"预算不足或未设置：已占用 {spent:g}，本次报价 {quote:g}")
            self.db.execute("UPDATE attempts SET quote=?,reserved=?,status='submitting',updated_at=? WHERE attempt_id=?", (quote, quote, now(), attempt_id))

    def spent(self):
        with self.lock:
            return self.db.execute("SELECT COALESCE(SUM(reserved),0) FROM attempts").fetchone()[0]

    def invalidate(self, node_ids):
        with self.lock, self.db:
            for node_id in node_ids:
                self.db.execute("UPDATE nodes SET revision=revision+1, previous_id=COALESCE(current_id,previous_id),current_id=NULL WHERE node_id=?", (node_id,))
                self.event(node_id, "invalidated", "operator requested new candidate")

    def event(self, node_id, event, detail=""):
        with self.lock, self.db:
            self.db.execute("INSERT INTO events(timestamp,node_id,event,detail) VALUES(?,?,?,?)", (now(), node_id, event, detail))

    def snapshot(self):
        with self.lock:
            return {"nodes": self.nodes(), "attempts": [self._decode(r) for r in self.db.execute("SELECT * FROM attempts ORDER BY created_at")], "budget_reserved_credits": self.spent()}

    def export_log(self):
        with self.lock:
            lines = [canonical(dict(row)) for row in self.db.execute("SELECT * FROM events ORDER BY event_id")]
        (self.root / "logs" / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
