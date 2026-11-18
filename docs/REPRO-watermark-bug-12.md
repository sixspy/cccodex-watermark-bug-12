# watermark-bug-12 复现与二验（另纸）

列绑定互斥：旁路映射表原子切换现行列，旧列整笔回滚，追索查只读现行映射。

## 一、旧 bug 复现（修复前代码）

1. `python run.py` 起服，登录 admin / 123456。
2. 调换绑接口：
   ```bash
   curl -X POST http://127.0.0.1:7000/api/templates/rebind \
     -H 'Content-Type: application/json' -b cookie.txt \
     -d '{"table_name":"dw_orders_d","column_name":"wm_batch_v2"}'
   ```
3. 三处病症同看：
   - **入库映射漂**：`SELECT * FROM meta;` 多出 `old_col_dw_orders_d=wm_batch`、
     `new_col_dw_orders_d=wm_batch_v2` 两条散键（旧列名硬编码回写）。
   - **旧列名挂在追索查链上**：`app/services/trace.py` 读 `old_col_%` 做
     `LIKE '%wm_batch%'` 模糊回退，追不存在的水印值也可能捞出不相干批次。
   - **互斥缺失**：模板原地改列、版本号不涨、无凭证、无锁；两人同时换绑
     各写各的映射，无人挡双列现行。

## 二、修复后二验步骤

### 1. 页面核现行列
- 模板页（列绑目录）：现行列亮青柠芯片，同行亮凭证标记（如
  `CRD-dw_orders_d-v2`），版本号随换绑 +1。
- 点「换绑」→ 输新列名 → 换绑进度提示「换绑中…」→「换绑完成 · 现行列
  X · 凭证 Y」；出错直接亮后端中文短句。
- 追索页跑一遍追索，底部「追索附页 · 现行列绑定」亮各表芯片 + 凭证标记。

### 2. 本机库可核
```bash
sqlite3 instance/watermark_lab.db
-- 每表恰一条现行映射（双列现行马上停）
SELECT table_name, COUNT(*) FROM column_bindings
 WHERE status='现行' GROUP BY table_name HAVING COUNT(*)>1;   -- 应 0 行
-- 模板与映射同源
SELECT t.table_name, t.column_name, b.column_name, b.credential
  FROM watermark_templates t
  JOIN column_bindings b ON b.table_name=t.table_name AND b.status='现行';
-- 旧列整笔回滚
SELECT * FROM column_bindings WHERE status='已回滚';
-- 漂移 meta 已摘除
SELECT * FROM meta WHERE key GLOB 'old_col_*' OR key GLOB 'new_col_*';  -- 应 0 行
```

### 3. 追索查复验两轮
- 换绑 `dw_orders_d` → `wm_ord_col` 后，连续两次追索 `WM-ORD-131`：
  两轮附页的列名与凭证标记完全一致，且与列绑目录对齐。
- 追不存在的水印值 → 404「未找到该水印值的盖印批次」，不再有旧列模糊回退。

### 4. 并发抢换绑（左右仓管）
- 两个终端同时对同一表点换绑（不同新列）：
  排他锁（BEGIN IMMEDIATE）串行化，先者成功后者排队或收
  「换绑冲突…」中文短句；事后查库现行映射仍恰一条，模板列名与映射一致。

### 5. 自动化
```bash
pytest tests/test_rebind.py -q
```
覆盖：页面核现行列、本机库核、两轮追索对齐、冲突短句、重复换绑无双列、
六线程并发抢换绑不变量。
