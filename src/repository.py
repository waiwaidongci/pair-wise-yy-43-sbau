from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES, WINDOW_STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        window_statuses = ",".join("'" + s.replace("'", "''") + "'" for s in WINDOW_STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS voyage_windows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    ship_name TEXT NOT NULL,
                    depart_at TEXT NOT NULL,
                    return_at TEXT NOT NULL,
                    estimated_recovery REAL NOT NULL,
                    sea_state INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ({window_statuses})),
                    reasons TEXT NOT NULL DEFAULT '[]',
                    actual_recovery REAL,
                    separated_oil REAL,
                    registered_by TEXT,
                    registered_at TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_voyage_windows_item
                    ON voyage_windows(item_id);
                CREATE INDEX IF NOT EXISTS ix_voyage_windows_ship
                    ON voyage_windows(ship_name, status);
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    @staticmethod
    def _window(row: sqlite3.Row) -> Dict[str, Any]:
        window = dict(row)
        window["reasons"] = json.loads(window["reasons"])
        return window

    def create_window(self, item_id: int, ship_name: str, depart_at: str,
                      return_at: str, estimated_recovery: float, sea_state: int,
                      status: str, reasons: List[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO voyage_windows(item_id, ship_name, depart_at, return_at,
                   estimated_recovery, sea_state, status, reasons, created_by,
                   created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (item_id, ship_name, depart_at, return_at, estimated_recovery,
                 sea_state, status, json.dumps(reasons, ensure_ascii=False),
                 actor, now, now),
            )
            window_id = int(cur.lastrowid)
        return self.get_window(window_id)

    def get_window(self, window_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM voyage_windows WHERE id=?", (window_id,)).fetchone()
        if row is None:
            raise NotFoundError("航窗口不存在")
        return self._window(row)

    def list_windows(self, item_id: Optional[int] = None,
                     status: Optional[str] = None,
                     ship_name: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM voyage_windows"
        clauses, params = [], []
        if item_id is not None:
            clauses.append("item_id=?")
            params.append(item_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        if ship_name:
            clauses.append("ship_name=?")
            params.append(ship_name)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [self._window(row) for row in rows]

    def confirm_window(self, window_id: int, depart_at: str, return_at: str,
                       estimated_recovery: float, sea_state: int, status: str,
                       reasons: List[str]) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE voyage_windows SET depart_at=?, return_at=?,
                   estimated_recovery=?, sea_state=?, status=?, reasons=?, updated_at=?
                   WHERE id=? AND status='pending_reschedule'""",
                (depart_at, return_at, estimated_recovery, sea_state, status,
                 json.dumps(reasons, ensure_ascii=False), now, window_id),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM voyage_windows WHERE id=?", (window_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("航窗口不存在")
                raise ConflictError("仅留待重排的航窗口可重新确认")
        return self.get_window(window_id)

    def register_window_return(self, window_id: int, actual_recovery: float,
                               separated_oil: float, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE voyage_windows SET status='completed', actual_recovery=?,
                   separated_oil=?, registered_by=?, registered_at=?, updated_at=?
                   WHERE id=? AND status='scheduled'""",
                (actual_recovery, separated_oil, actor, now, now, window_id),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM voyage_windows WHERE id=?", (window_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("航窗口不存在")
                raise ConflictError("仅已排期的航窗口可回港登记")
        return self.get_window(window_id)

    def recovery_progress(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                """SELECT COALESCE(SUM(CASE WHEN status IN ('scheduled','completed')
                            THEN estimated_recovery ELSE 0 END),0) AS estimated_total,
                          COALESCE(SUM(CASE WHEN status='completed'
                            THEN actual_recovery ELSE 0 END),0) AS recovered_total,
                          COALESCE(SUM(CASE WHEN status='completed'
                            THEN separated_oil ELSE 0 END),0) AS separated_oil_total
                   FROM voyage_windows WHERE item_id=?""",
                (item_id,),
            ).fetchone()
        return {"estimated_total": row["estimated_total"],
                "recovered_total": row["recovered_total"],
                "separated_oil_total": row["separated_oil_total"]}

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
