import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 写锁等待：并发换绑时让后者短暂排队而非立刻报错
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def get_db(db_path: str | None = None, immediate: bool = False):
    conn = connect(db_path)
    try:
        if immediate:
            # 排他写锁：进入即持锁，串行化并发写（如左右仓管同时换绑）
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS watermark_templates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL UNIQUE,
  column_name TEXT NOT NULL,
  version_no INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL CHECK (status IN ('现行','停用')),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS run_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  run_code TEXT NOT NULL UNIQUE,
  biz_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('待盖印','已盖印','已作废')),
  watermark_value TEXT,
  stamped_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lineage_edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  upstream_run_code TEXT NOT NULL,
  downstream_run_code TEXT NOT NULL,
  UNIQUE(upstream_run_code, downstream_run_code)
);

CREATE TABLE IF NOT EXISTS gap_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of_date TEXT NOT NULL,
  expected_count INTEGER NOT NULL,
  stamped_count INTEGER NOT NULL,
  gap_count INTEGER NOT NULL,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS column_bindings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  column_name TEXT NOT NULL,
  credential TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK (status IN ('现行','已回滚')),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 列绑定互斥：同一表只允许一条现行映射，双列现行直接挡在库层
CREATE UNIQUE INDEX IF NOT EXISTS uq_column_bindings_current
  ON column_bindings(table_name) WHERE status='现行';
"""

SEED_TEMPLATES = [
    ("dw_orders_d", "wm_batch", 1, "现行"),
    ("dw_payments_d", "wm_batch", 1, "现行"),
    ("dm_gmv_d", "wm_trace", 1, "现行"),
]

SEED_BATCHES = [
    ("dw_orders_d", "ord-20261114-01", "2026-11-14", "待盖印", None),
    ("dw_payments_d", "pay-20261114-01", "2026-11-14", "待盖印", None),
    ("dm_gmv_d", "gmv-20261114-01", "2026-11-14", "待盖印", None),
    ("dw_orders_d", "ord-20261113-01", "2026-11-13", "已盖印", "WM-ORD-131"),
]

SEED_EDGES = [
    ("ord-20261114-01", "gmv-20261114-01"),
    ("pay-20261114-01", "gmv-20261114-01"),
    ("ord-20261113-01", "gmv-20261114-01"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    # 摘掉旧版换绑留下的漂移 meta 映射，旧列名不得再挂在追索链上
    conn.execute("DELETE FROM meta WHERE key GLOB 'old_col_*' OR key GLOB 'new_col_*'")
    # 旧库升级：为缺少现行映射的现行模板补登绑定（模板与映射同源）
    rows = conn.execute(
        "SELECT t.* FROM watermark_templates t "
        "WHERE t.status='现行' AND NOT EXISTS ("
        "  SELECT 1 FROM column_bindings b "
        "  WHERE b.table_name=t.table_name AND b.status='现行')",
    ).fetchall()
    for tpl in rows:
        conn.execute(
            "INSERT INTO column_bindings(table_name, column_name, credential, status) "
            "VALUES (?,?,?,'现行')",
            (
                tpl["table_name"],
                tpl["column_name"],
                f"CRD-{tpl['table_name']}-v{tpl['version_no']}",
            ),
        )


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone()
        if not row:
            for table, col, ver, status in SEED_TEMPLATES:
                conn.execute(
                    "INSERT INTO watermark_templates(table_name, column_name, version_no, status) "
                    "VALUES (?,?,?,?)",
                    (table, col, ver, status),
                )
            for table, code, biz, status, wm in SEED_BATCHES:
                conn.execute(
                    "INSERT INTO run_batches(table_name, run_code, biz_date, status, watermark_value, stamped_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        table,
                        code,
                        biz,
                        status,
                        wm,
                        "2026-11-13 18:00:00" if status == "已盖印" else None,
                    ),
                )
            for up, down in SEED_EDGES:
                conn.execute(
                    "INSERT INTO lineage_edges(upstream_run_code, downstream_run_code) VALUES (?,?)",
                    (up, down),
                )
            conn.execute("INSERT INTO meta(key, value) VALUES ('seeded','1')")
        _migrate(conn)
