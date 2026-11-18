"""换绑互斥（watermark-bug-12）验收测试。

覆盖：页面核现行列、本机库可核、追索查两轮复验列名与凭证标记对齐、
并发换绑排他锁、双列现行即停、中文冲突短句、旧列名脱离追索链。
"""

import os
import sqlite3
import threading

import pytest

from app.services.errors import ServiceError
from app.services.rebind import rebind_column


def _db_path() -> str:
    return os.environ["DATABASE_PATH"]


def _sql(query: str, args=()) -> list[dict]:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(query, args).fetchall()]
    finally:
        conn.close()


def _current_binding(table: str) -> dict | None:
    rows = _sql(
        "SELECT * FROM column_bindings WHERE table_name=? AND status='现行'", (table,)
    )
    assert len(rows) <= 1, "双列现行：同一表出现多条现行映射"
    return rows[0] if rows else None


@pytest.mark.asyncio
async def test_rebind_page_and_db_consistent(authed):
    r = await authed.post(
        "/api/templates/rebind",
        json={"table_name": "dw_orders_d", "column_name": "wm_batch_v2"},
    )
    assert r.status == 200
    body = await r.json()
    assert body["ok"] is True
    assert body["column_name"] == "wm_batch_v2"
    assert body["version_no"] == 2
    assert body["credential"] == "CRD-dw_orders_d-v2"

    # 页面核现行列：列绑目录接口亮出现行列芯片与凭证标记
    tpls = (await (await authed.get("/api/templates")).json())["items"]
    row = next(t for t in tpls if t["table_name"] == "dw_orders_d")
    assert row["column_name"] == "wm_batch_v2"
    assert row["bound_column"] == "wm_batch_v2"
    assert row["credential"] == "CRD-dw_orders_d-v2"
    assert row["version_no"] == 2
    # 其余现行模板经迁移补登，同样带凭证标记
    assert all(t["credential"] for t in tpls if t["status"] == "现行")

    # 本机库可核：唯一现行映射、旧列整笔回滚、模板与映射同源
    cur = _current_binding("dw_orders_d")
    assert cur["column_name"] == "wm_batch_v2"
    assert cur["credential"] == "CRD-dw_orders_d-v2"
    old = _sql(
        "SELECT * FROM column_bindings WHERE table_name='dw_orders_d' AND status='已回滚'"
    )
    assert len(old) == 1 and old[0]["column_name"] == "wm_batch"
    tpl = _sql("SELECT * FROM watermark_templates WHERE table_name='dw_orders_d'")[0]
    assert tpl["column_name"] == cur["column_name"]
    assert tpl["version_no"] == 2
    # 旧版漂移 meta 已摘除
    assert not _sql(
        "SELECT * FROM meta WHERE key GLOB 'old_col_*' OR key GLOB 'new_col_*'"
    )


@pytest.mark.asyncio
async def test_trace_appendix_two_rounds_aligned(authed):
    await authed.post(
        "/api/templates/rebind",
        json={"table_name": "dw_orders_d", "column_name": "wm_ord_col"},
    )
    rounds = []
    for _ in range(2):  # 追索查复验两轮
        resp = await authed.post("/api/trace", json={"watermark_value": "WM-ORD-131"})
        assert resp.status == 200
        rounds.append(await resp.json())
    b1 = rounds[0]["bindings"]
    b2 = rounds[1]["bindings"]
    assert b1 and b1 == b2, "两轮追索的列名与凭证标记必须一致"
    bind = next(b for b in b1 if b["table_name"] == "dw_orders_d")
    assert bind["column_name"] == "wm_ord_col"
    assert bind["credential"] == "CRD-dw_orders_d-v2"
    # 与列绑目录对齐
    tpls = (await (await authed.get("/api/templates")).json())["items"]
    row = next(t for t in tpls if t["table_name"] == "dw_orders_d")
    assert row["bound_column"] == bind["column_name"]
    assert row["credential"] == bind["credential"]


@pytest.mark.asyncio
async def test_trace_unknown_value_404_no_old_column_fallback(authed):
    await authed.post(
        "/api/templates/rebind",
        json={"table_name": "dw_orders_d", "column_name": "wm_batch_v9"},
    )
    resp = await authed.post("/api/trace", json={"watermark_value": "WM-NOPE-999"})
    assert resp.status == 404
    assert "未找到" in (await resp.json())["error"]


@pytest.mark.asyncio
async def test_rebind_conflict_short_messages(authed):
    same = await authed.post(
        "/api/templates/rebind",
        json={"table_name": "dw_orders_d", "column_name": "wm_batch"},
    )
    assert same.status == 409
    assert "无需换绑" in (await same.json())["error"]

    empty = await authed.post(
        "/api/templates/rebind", json={"table_name": "dw_orders_d", "column_name": " "}
    )
    assert empty.status == 400
    assert "新列名不能为空" in (await empty.json())["error"]

    missing = await authed.post(
        "/api/templates/rebind",
        json={"table_name": "dw_ghost_d", "column_name": "wm_x"},
    )
    assert missing.status == 404
    assert "无法换绑" in (await missing.json())["error"]


@pytest.mark.asyncio
async def test_repeat_rebind_never_dual_current(authed):
    for col in ("wm_v2", "wm_v3", "wm_v4"):
        r = await authed.post(
            "/api/templates/rebind",
            json={"table_name": "dm_gmv_d", "column_name": col},
        )
        assert r.status == 200
    # 双列现行马上停：任何时刻只有一条现行映射
    cur = _current_binding("dm_gmv_d")
    assert cur["column_name"] == "wm_v4"
    assert cur["credential"] == "CRD-dm_gmv_d-v4"
    rolled = _sql(
        "SELECT COUNT(*) AS c FROM column_bindings "
        "WHERE table_name='dm_gmv_d' AND status='已回滚'"
    )[0]["c"]
    assert rolled == 3


@pytest.mark.asyncio
async def test_concurrent_rebind_serialized(client):
    """左右仓管并发抢换绑：排他锁串行化，绝不多写现行映射行。"""
    path = _db_path()
    columns = [f"wm_race_{i}" for i in range(6)]
    results, errors = [], []

    def worker(col):
        try:
            results.append(rebind_column("dw_payments_d", col, path))
        except ServiceError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(c,)) for c in columns]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results, "至少一方换绑成功"
    assert all(e.status_code == 409 for e in errors)
    # 不变量：唯一现行映射，模板与映射同源，凭证不重复
    cur = _current_binding("dw_payments_d")
    tpl = _sql("SELECT * FROM watermark_templates WHERE table_name='dw_payments_d'")[0]
    assert tpl["column_name"] == cur["column_name"]
    assert tpl["version_no"] == 1 + len(results)
    creds = [r["credential"] for r in results]
    assert len(set(creds)) == len(creds)
    total_current = _sql(
        "SELECT COUNT(*) AS c FROM column_bindings WHERE status='现行'"
    )[0]["c"]
    assert total_current == 3  # 三张种子表各一条，绝无双列现行
