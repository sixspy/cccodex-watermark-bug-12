from app.db import get_db
from app.services.errors import ServiceError


def trace_downstream(watermark_value: str, db_path: str | None = None) -> dict:
    """按水印值追索下游批次。

    只认精确水印值与现行列绑定；旧列名的 meta 模糊回退已摘除，
    追索附页返回源批次各表的现行列与凭证标记。
    """
    value = (watermark_value or "").strip()
    if not value:
        raise ServiceError("水印值不能为空")
    with get_db(db_path) as conn:
        sources = conn.execute(
            "SELECT * FROM run_batches WHERE watermark_value=? ORDER BY id",
            (value,),
        ).fetchall()
        if not sources:
            raise ServiceError("未找到该水印值的盖印批次", 404)
        codes = [s["run_code"] for s in sources]
        placeholders = ",".join("?" for _ in codes)
        edges = conn.execute(
            f"SELECT * FROM lineage_edges WHERE upstream_run_code IN ({placeholders})",
            codes,
        ).fetchall()
        down_codes = sorted({e["downstream_run_code"] for e in edges})
        downstream = []
        if down_codes:
            ph2 = ",".join("?" for _ in down_codes)
            downstream = [
                dict(r)
                for r in conn.execute(
                    f"SELECT * FROM run_batches WHERE run_code IN ({ph2}) ORDER BY id",
                    down_codes,
                ).fetchall()
            ]
        # 追索附页：源批次各表的现行列绑定（列名 + 凭证标记），只读现行映射
        table_names = sorted({s["table_name"] for s in sources})
        ph3 = ",".join("?" for _ in table_names)
        bindings = [
            dict(r)
            for r in conn.execute(
                f"SELECT table_name, column_name, credential FROM column_bindings "
                f"WHERE status='现行' AND table_name IN ({ph3}) ORDER BY table_name",
                table_names,
            ).fetchall()
        ]
        return {
            "watermark_value": value,
            "sources": [dict(s) for s in sources],
            "edges": [dict(e) for e in edges],
            "downstream": downstream,
            "bindings": bindings,
        }
