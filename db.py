"""SQLite storage (pengganti D1 di CF Worker): kv, users, request_logs.

Multi-proses: bot.py & app.py berbagi satu file SQLite (WAL mode).
Path diambil dari env SQLITE_PATH (default /app/data/bypass.db).
"""

import os
import sqlite3
import threading

_DEFAULT_PATH = "/app/data/bypass.db"
_connections: dict = {}
_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  username TEXT DEFAULT '',
  first_name TEXT DEFAULT '',
  joined_at INTEGER NOT NULL,
  request_count INTEGER DEFAULT 0,
  last_active INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS request_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  command TEXT NOT NULL,
  platform TEXT DEFAULT '',
  url TEXT DEFAULT '',
  status TEXT DEFAULT 'ok',
  ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);
"""


def db_path() -> str:
    return os.environ.get("SQLITE_PATH", _DEFAULT_PATH)


def get_conn() -> sqlite3.Connection:
    path = db_path()
    with _lock:
        conn = _connections.get(path)
        if conn is None:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            conn = sqlite3.connect(path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.executescript(_SCHEMA)
            conn.commit()
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.Error:
                pass
            _connections[path] = conn
        return conn


def kv_set(key: str, value, ttl: int | None = None) -> None:
    import json
    raw = value if isinstance(value, str) else json.dumps(value)
    conn = get_conn()
    with conn:
        conn.execute(
            "INSERT INTO kv (k, v, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
            (key, raw, int(now_ms())),
        )


def kv_get(key: str, ttl_ms: int | None = None):
    import json
    conn = get_conn()
    row = conn.execute(
        "SELECT v, updated_at FROM kv WHERE k = ?", (key,)
    ).fetchone()
    if not row:
        return None
    if ttl_ms is not None and now_ms() - int(row["updated_at"]) > ttl_ms:
        return None
    val = row["v"]
    try:
        return json.loads(val)
    except (ValueError, TypeError):
        return val


def kv_del(key: str) -> None:
    conn = get_conn()
    with conn:
        conn.execute("DELETE FROM kv WHERE k = ?", (key,))


def upsert_user(user_id: int, username: str, first_name: str) -> None:
    conn = get_conn()
    with conn:
        conn.execute(
            "INSERT INTO users (id, username, first_name, joined_at, last_active, request_count) "
            "VALUES (?, ?, ?, ?, ?, 1) "
            "ON CONFLICT(id) DO UPDATE SET "
            "username=excluded.username, first_name=excluded.first_name, "
            "last_active=excluded.last_active, request_count=request_count+1",
            (int(user_id), username or "", first_name or "", int(now_ms()), int(now_ms())),
        )


def log_request(user_id: int, command: str, platform: str, url: str, status: str) -> None:
    conn = get_conn()
    with conn:
        conn.execute(
            "INSERT INTO request_logs (user_id, command, platform, url, status, ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (int(user_id), command, platform, url, status, int(now_ms())),
        )


def get_stats() -> dict:
    conn = get_conn()
    total_users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] or 0
    today_requests = conn.execute(
        "SELECT COUNT(*) AS c FROM request_logs WHERE ts >= ?", (start_of_today_ms(),)
    ).fetchone()["c"] or 0
    week_requests = conn.execute(
        "SELECT COUNT(*) AS c FROM request_logs WHERE ts >= ?", (int(now_ms()) - 7 * 86400000,)
    ).fetchone()["c"] or 0
    failed = conn.execute(
        "SELECT COUNT(*) AS c FROM request_logs WHERE ts >= ? AND status != 'ok'",
        (start_of_today_ms(),),
    ).fetchone()["c"] or 0
    success_rate = round((today_requests - failed) / today_requests * 100) if today_requests else 100
    return {
        "total_users": total_users,
        "today_requests": today_requests,
        "week_requests": week_requests,
        "success_rate": success_rate,
    }


def list_users(limit: int = 50) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, username, first_name, request_count, last_active "
        "FROM users ORDER BY request_count DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def all_user_ids() -> list[int]:
    conn = get_conn()
    rows = conn.execute("SELECT id FROM users").fetchall()
    return [int(r["id"]) for r in rows]


def count_users() -> int:
    conn = get_conn()
    return conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] or 0


def list_users_detailed(page_size: int, offset: int) -> list:
    """User per halaman + rekap request per platform (hanya status='ok')."""
    conn = get_conn()
    users = conn.execute(
        "SELECT id, username, first_name, request_count, last_active "
        "FROM users ORDER BY request_count DESC, last_active DESC LIMIT ? OFFSET ?",
        (page_size, offset),
    ).fetchall()
    if not users:
        return []
    ids = [int(u["id"]) for u in users]
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT user_id, platform, COUNT(*) AS c FROM request_logs "
        f"WHERE user_id IN ({placeholders}) AND status = 'ok' "
        f"GROUP BY user_id, platform",
        ids,
    ).fetchall()
    by_user: dict = {}
    for r in rows:
        by_user.setdefault(int(r["user_id"]), {})[r["platform"]] = int(r["c"])
    out = []
    for u in users:
        d = dict(u)
        d["platforms"] = by_user.get(int(u["id"]), {})
        out.append(d)
    return out


_reset_done = False


def reset_if_requested() -> None:
    """Sekali per proses: wipe users + request_logs jika env RESET_USERS='1'."""
    global _reset_done
    if _reset_done:
        return
    _reset_done = True
    if os.environ.get("RESET_USERS") == "1":
        conn = get_conn()
        with conn:
            conn.execute("DELETE FROM request_logs")
            conn.execute("DELETE FROM users")


reset_if_requested()


def now_ms() -> int:
    import time
    return int(time.time() * 1000)


def start_of_today_ms() -> int:
    import time
    utc = time.gmtime()
    day = time.mktime((utc.tm_year, utc.tm_mon, utc.tm_mday, 0, 0, 0, 0, 0, 0))
    return int(day * 1000 + 7 * 3600 * 1000)