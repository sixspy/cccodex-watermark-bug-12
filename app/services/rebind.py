import sqlite3

from app.db import get_db
from app.services.errors import ServiceError


def rebind_column(table_name: str, new_column: str, db_path: str | None = None) -> dict:
    """换绑现行列：旁路映射表原子切换。

    一个事务内完成：旧列整笔回滚 → 新列写入现行映射 → 模板同源升级，
    按凭证一次写库。BEGIN IMMEDIATE 持排他锁，并发换绑串行化；
    部分唯一索引兜底，双列现行在库层被挡下并回中文冲突短句。
    """
    table = (table_name or "").strip()
    column = (new_column or "").strip()
    if not table:
        raise ServiceError("表名不能为空")
    if not column:
        raise ServiceError("新列名不能为空")
    try:
        with get_db(db_path, immediate=True) as conn:
            tpl = conn.execute(
                "SELECT * FROM watermark_templates WHERE table_name=? AND status='现行'",
                (table,),
            ).fetchone()
            if not tpl:
                raise ServiceError("无现行水印模板，无法换绑", 404)
            if tpl["column_name"] == column:
                raise ServiceError("新列与现行列相同，无需换绑", 409)
            version_no = tpl["version_no"] + 1
            credential = f"CRD-{table}-v{version_no}"
            # 旧列整笔回滚：追索查不再读到旧列
            conn.execute(
                "UPDATE column_bindings SET status='已回滚' "
                "WHERE table_name=? AND status='现行'",
                (table,),
            )
            conn.execute(
                "INSERT INTO column_bindings(table_name, column_name, credential, status) "
                "VALUES (?,?,?,'现行')",
                (table, column, credential),
            )
            # 新列与版式同源：同事务同一份值升级模板
            conn.execute(
                "UPDATE watermark_templates SET column_name=?, version_no=? "
                "WHERE id=? AND status='现行'",
                (column, version_no, tpl["id"]),
            )
            return {
                "table_name": table,
                "column_name": column,
                "version_no": version_no,
                "credential": credential,
                "ok": True,
            }
    except sqlite3.IntegrityError:
        # 唯一索引挡住双列现行
        raise ServiceError("换绑冲突：该表已存在现行列绑定，请刷新后重试", 409)
    except sqlite3.OperationalError:
        # 排他锁等待超时
        raise ServiceError("换绑冲突：并发换绑繁忙，请稍后重试", 409)
