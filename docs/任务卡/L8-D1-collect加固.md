# 任务卡 · L8-D1 collect 超时加固

> 状态：**已完成**（2026-10-26）· 归属阶段：L8

## 1. 问题（实测，不是推测）

2026-10-26 Actions 多次出现：

```
Collect  │ arxiv │ 0 │ ... │ warning ReadTimeout:
```

7 天窗口里**任意一天**读超时 ⇒ 整个 arXiv 源 `fetched=0` —— 当日新增直接归零。
根因两条：

1. 全局 `http.timeout_read_s=20`，而 arXiv 单页 200 条在高峰期常 >20s；
2. `_collect_source` 把整源当一个事务：一天失败 = 整源抛异常。

## 2. 修法

| # | 改动 | 位置 |
|---|---|---|
| 1 | arXiv 读超时**独立可配**（默认 60s），不抬高其他源 | `collect.arxiv_read_timeout_s` + `fetch_arxiv_day(read_timeout_s=...)` |
| 2 | **逐日隔离**：某天失败只记 `degraded['{source}_partial']`，其余天照常入库 | `_collect_source` 返回 `(items, missing, failures)` |
| 3 | 全部天失败仍按"源失败"处理（复用原 key，报告/Dashboard 照旧标黄） | `run_collect` |

★ 语义分界：**部分失败** ⇒ `arxiv_partial`（不影响 `ok`）；**全量失败** ⇒ `arxiv`（`ok=False`）。
两者都不改退出码（沿用"单源降级不影响退出码"的红线）。

## 3. DoD

1. `Settings().collect.arxiv_read_timeout_s >= 30` 且能覆盖全局 20s；
2. 2 天窗口里 1 天超时 ⇒ `fetched == 1`、`arxiv_partial` 含日期与异常名；
3. 2 天都超时 ⇒ `outcome.failed=True`、`report.ok=False`、`degraded['arxiv']` 有原因；
4. 单测覆盖以上三条（`tests/test_collect.py`）。

## 4. 结果

- 配置：`collect.arxiv_read_timeout_s: 60`（`config/default.yaml` + `CollectSettings`）。
- `fetch_arxiv_day(..., read_timeout_s=60)` 用 `httpx.Timeout(connect=..., read=60)`。
- 逐日隔离 + 全量/部分失败语义区分；3 条新单测。
