"""SQLite persistence (WAL) with versioned migrations and pre-migration backups.

The database lives in the user data directory, never inside the build/dist
folder, so rebuilding the executable can never destroy history.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from predictionbot.buildinfo import SCHEMA_VERSION

log = logging.getLogger(__name__)
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


class DatabaseError(RuntimeError):
    pass


def _migrations() -> list[tuple[int, Path]]:
    out = []
    for p in sorted(MIGRATIONS_DIR.glob("*.sql")):
        m = re.match(r"(\d+)_", p.name)
        if m:
            out.append((int(m.group(1)), p))
    return out


class Database:
    def __init__(self, path: Path, backup_dir: Path | None = None) -> None:
        self.path = path
        self.backup_dir = backup_dir or path.parent / "backups"
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self.last_error: str | None = None
        self.last_write_ts: float | None = None
        self.writes = 0
        self.failures = 0

    # --------------------------------------------------------------- setup
    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        self._conn = conn
        self.migrate()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise DatabaseError("database not open")
        return self._conn

    def schema_version(self) -> int:
        try:
            row = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        except sqlite3.OperationalError:
            return 0
        return int(row[0]) if row else 0

    def backup(self, label: str) -> Path | None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return None
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        dest = self.backup_dir / f"{self.path.stem}-{stamp}-{label}.db"
        target = sqlite3.connect(dest)
        try:
            self.conn.backup(target)
        finally:
            target.close()
        log.info("database backed up to %s", dest)
        return dest

    def migrate(self) -> None:
        current = self.schema_version()
        pending = [(v, p) for v, p in _migrations() if v > current]
        if current > SCHEMA_VERSION:
            raise DatabaseError(
                f"database schema v{current} is newer than this build (v{SCHEMA_VERSION}); refusing to run"
            )
        if not pending:
            return
        if current > 0:
            self.backup(f"pre-v{pending[-1][0]}")
        with self._lock:
            for version, path in pending:
                sql = path.read_text(encoding="utf-8")
                self.conn.execute("BEGIN")
                try:
                    self._exec_script(sql)
                    self.conn.execute(
                        "INSERT INTO meta(key,value) VALUES('schema_version',?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (str(version),),
                    )
                    self.conn.execute("COMMIT")
                except Exception:
                    self.conn.execute("ROLLBACK")
                    raise
                log.info("applied migration %s", path.name)

    def _exec_script(self, sql: str) -> None:
        # executescript() would COMMIT implicitly; run statements one by one inside our txn.
        for stmt in [s.strip() for s in sql.split(";")]:
            body = "\n".join(line for line in stmt.splitlines() if not line.strip().startswith("--")).strip()
            if body:
                self.conn.execute(body)

    # ----------------------------------------------------------------- ops
    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self._lock:
            try:
                cur = self.conn.execute(sql, params)
            except sqlite3.Error as exc:
                self.failures += 1
                self.last_error = f"{exc.__class__.__name__}: {exc}"
                raise DatabaseError(self.last_error) from exc
            self.writes += 1
            self.last_write_ts = time.time()
            return int(cur.lastrowid or 0)

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        with self._lock:
            try:
                self.conn.execute("BEGIN")
                self.conn.executemany(sql, rows)
                self.conn.execute("COMMIT")
            except sqlite3.Error as exc:
                try:
                    self.conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                self.failures += 1
                self.last_error = f"{exc.__class__.__name__}: {exc}"
                raise DatabaseError(self.last_error) from exc
            self.writes += 1
            self.last_write_ts = time.time()

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            try:
                return [dict(r) for r in self.conn.execute(sql, params).fetchall()]
            except sqlite3.Error as exc:
                self.failures += 1
                self.last_error = f"{exc.__class__.__name__}: {exc}"
                raise DatabaseError(self.last_error) from exc

    def one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def healthy(self) -> tuple[bool, str]:
        try:
            self.query("SELECT 1")
        except DatabaseError as exc:
            return False, str(exc)
        return True, "ok"

    def insert(self, table: str, row: dict[str, Any]) -> int:
        if not re.fullmatch(r"[a-z_]+", table):
            raise DatabaseError("bad table name")
        cols = list(row)
        for c in cols:
            if not re.fullmatch(r"[a-z_0-9]+", c):
                raise DatabaseError("bad column name")
        vals = [_ser(row[c]) for c in cols]
        sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"  # noqa: S608
        return self.execute(sql, vals)

    def upsert(self, table: str, row: dict[str, Any], keys: Sequence[str]) -> None:
        if not re.fullmatch(r"[a-z_]+", table):
            raise DatabaseError("bad table name")
        cols = list(row)
        for c in cols:
            if not re.fullmatch(r"[a-z_0-9]+", c):
                raise DatabaseError("bad column name")
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c not in keys)
        sql = (
            f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "  # noqa: S608
            f"ON CONFLICT({','.join(keys)}) DO UPDATE SET {updates}"
        )
        self.execute(sql, [_ser(row[c]) for c in cols])


def _ser(v: Any) -> Any:
    if v is None or isinstance(v, (int, float, str, bytes)):
        return v
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (dict, list, tuple)):
        return json.dumps(v, default=str)
    return str(v)
