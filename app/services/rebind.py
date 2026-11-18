from app.db import get_db
from app.services.errors import ServiceError


def rebind_column(table_name: str, new_column: str, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        # silent: update template but keep meta old_column for trace
        conn.execute(
            "UPDATE watermark_templates SET column_name=? WHERE table_name=? AND status='现行'",
            (new_column, table_name),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            (f"old_col_{table_name}", "wm_batch"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            (f"new_col_{table_name}", new_column),
        )
        return {"table_name": table_name, "column_name": new_column, "ok": True}
