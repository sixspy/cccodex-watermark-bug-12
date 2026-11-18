from app.db import get_db


def list_templates(db_path: str | None = None, status: str | None = None) -> list[dict]:
    """列绑目录：模板左联现行映射，带出现行列与凭证标记。"""
    base = (
        "SELECT t.*, b.column_name AS bound_column, b.credential AS credential "
        "FROM watermark_templates t "
        "LEFT JOIN column_bindings b "
        "  ON b.table_name=t.table_name AND b.status='现行' "
    )
    with get_db(db_path) as conn:
        if status:
            rows = conn.execute(
                base + "WHERE t.status=? ORDER BY t.id",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(base + "ORDER BY t.id").fetchall()
        return [dict(r) for r in rows]
