# 任务卡 · L9-E 深度分析配额削峰（日 1 篇 / 周 5 篇）

> 状态：**已完成**（2026-11-05 实施完成；待 2026-11-09 周一 Actions 实测验收单日费用）
> 归属阶段：L9（运营期，按需维护；负责人 2026-11-05 批准）
> 依据：负责人 2026-11-05 指令「批准方案 1 并出任务卡，先列预算与 diff 再动手，授权本地 commit」
> 根因证据：GitHub `data/reports/daily/2026-11-02.json` + Actions run `41348306339` / `41547147555` 日志
> 关联：`docs/成本预算.md` §9.4（稳态表「analyze 1 篇/天」）· `CODEBUDDY.md` 红线 8 · 治理协议 L3（成本口径变更，需负责人批准）
> 授权边界：本地 commit ✅ · push ❌ · tag ❌ · 任何会调 LLM 的命令 ❌

---

## 1. 背景：11/2 的 ¥0.3108 不是 bug，是实现偏离了设计意图

| 证据 | 值 |
|---|---|
| `2026-11-02.json` | `cost_cny = ¥0.3108`；`deep_analyzed` **5 篇**（10/31~11/04 唯一非空 day）；自带告警 `> 近 7 日中位数 ¥0.0552 × 3` |
| run `41348306339`（UTC 11-01 20:43 = **北京 11/02 04:43**） | `enhance ¥0.0310` + `grade ¥0.0056` + **`analyze`(strong) ¥0.2743**（占 88%，6 分钟）；Cost guard「今日 ¥0.3108」 |
| run `41547147555`（北京 11/03） | `analyze` 报 `warning quota：周配额已满（5 篇）`、`--check`「本次候选 10 篇」排队；Cost guard「今日 ¥0.0290」 |

因果链：`deep_analysis.weekly_limit = 5`，窗口是**自然周**（`llm_support.window_starts` 用 `day.weekday()` 取周一为起点）⇒ 11/2 周一重置，而单次 run 的 `default_limit = 5` 允许**一次把整周额度跑完** ⇒ 单日 ¥0.3108 尖峰。10/26 同为周一（¥0.9709，叠加 L8 调试重跑），属同一结构性现象。

★ `docs/成本预算.md` §9.4 稳态表从一开始就写的是 **「analyze（1 篇/天，¥0.05）」**。
⇒ 本次不是新增限制，而是**让实现回到已定案的设计口径**。

## 2. 选型：取「每天最多 1 篇」，排除「滚动 7 天窗口」

| 候选 | 判定 | 理由 |
|---|---|---|
| **日配额 `daily_limit = 1`** | ✅ **采纳** | 直接约束单次 run 的产出篇数，是唯一能削掉「一天 ¥0.31」的手段；与 §9.4 的 1 篇/天口径一致 |
| 滚动 7 天窗口 | ❌ 排除 | 只改「何时重置」，**不改单次 run 可跑满 5 篇**（`default_limit=5`）。11/2 那种「窗口内 0 篇 ⇒ 一次跑满 5 篇」仍会复现，只是尖峰从固定周一变成随机日 ⇒ 削不了峰 |
| 改 `default_limit` 5→1 | ❌ 排除 | 它同时是 CLI `--limit` 默认值，会让手动 `analyze --ids a,b` 也只分析第 1 篇，破坏既有用法 |

设计要点：
- **双层配额并存**：`weekly_limit = 5`（周总量）+ `daily_limit = 1`（日削峰）。周总成本不变，只是摊到至少 5 天。
- **不动** `default_limit`（单次候选池上限）、不动任何单价、不动 `budget.*_limit_cny` 金额闸门、不动 `enhance` / `grade`。
- 配额口径仍是**已产出篇数**（`papers.deep_analyzed_at`），不是调用次数 —— 沿用 2026-10-26 的 E9 修复，废调用不烧额度。
- 超限**进队列不丢弃**（`blocked`），积压由后续日自然消化；`--check` 出现「候选 10 篇」属预期，不是 bug。

## 3. 预算（★ 动手前先列）

### 3.1 成本影响

| 项 | 改前（实测 11/2） | 改后 | 变化 |
|---|---|---|---|
| `analyze` 单篇 | ¥0.2743 / 5 = **¥0.0549** | 同左 | 0 |
| 单日峰值 | ¥0.3108 | ¥0.0310 + ¥0.0056 + ¥0.0549 ≈ **¥0.0915** | **−70.6%** |
| 周总额度 | 5 篇 = ¥0.2743 | 5 篇 = ¥0.2743 | **0**（`weekly_limit` 不变） |
| 日均（摊 7 天） | — | ≈ ¥0.0392 | 与 §9.4「1 篇/天 ¥0.05」同量级 |
| 年成本 | ≈ ¥52/年（analyze 占 ¥18） | 不变 | **0** |
| 异常告警 | 11/2 触发「> 中位数 × 3」 | ¥0.0915 < ¥0.166 ⇒ **不再误报** | 告警回归真实异常 |

★ 结论：**只搬动成本在时间轴上的分布，不增加、不减少总支出**。年成本仍 ≈¥52，仍在 `budget.yearly_limit_cny = 200` 内。

### 3.2 本次改动自身花费

**¥0**。纯本地逻辑 + 单元测试：无新增 LLM 调用点、无真实 API 请求（`respx` mock / `FakeDeepLLM`）、`pytest` 离线跑、`gh` 只读、`ai-scholar cost` 零 LLM。不跑 `collect` / `enhance` / `grade` / `analyze` 实跑。

### 3.3 代价（可接受）

- 积压候选消化变慢：原来周一一天清空 5 篇 → 现在至少 5 天。`blocked` / `--check` 的候选数会持续 >0，属队列正常状态。
- 新增一个配置项与一个 `degraded` key（`daily_quota`），需要同步 4 份活文档。

## 4. 精确 diff（★ 动手前先列，实施严格按此执行）

### 4.1 `src/ai_scholar/config.py`（`DeepAnalysisSettings`）

```diff
 class DeepAnalysisSettings(BaseModel):
-    """L3b-2 深度分析参数（strong）。★ 注意力预算：每周最多 N 篇。"""
+    """L3b-2 深度分析参数（strong）。★ 双层注意力预算：每日 N 篇 + 每周 M 篇。"""

     weekly_limit: int = 5
+    daily_limit: int = 1
     default_limit: int = 5
```

### 4.2 `config/default.yaml`（`deep_analysis:` 段）

```diff
 deep_analysis:
-  # ★ 注意力预算：每周最多 5 篇 strong 分析；超限进队列，不降单篇质量
+  # ★ 双层注意力预算：每周最多 5 篇；每日最多 1 篇（削峰，避免周一集中跑满整周额度）
   weekly_limit: 5
+  daily_limit: 1
   default_limit: 5
```

### 4.3 `src/ai_scholar/pipeline/deep_analysis.py`

① 文件头注释补一句双层预算说明。

② 新增 `_daily_analyzed`（紧邻 `_weekly_analyzed`），口径与 `pipeline/report.py` 的「当日深度分析」一致：

```python
def _daily_analyzed(session: Session, *, today: date) -> int:
    """当日**已产出**的深度分析篇数（日配额口径 = 篇，与周配额同）。

    ★ 用 `>=` 而非 `==`：容忍历史上带时间戳的 `deep_analyzed_at`，与 `_weekly_analyzed` 风格一致。
    """
    rows = session.exec(select(Paper).where(col(Paper.deep_analyzed_at) >= today.isoformat())).all()
    return len(rows)
```

③ `run_deep_analysis` 取配置、算初始计数：

```diff
     weekly_limit = settings.deep_analysis.weekly_limit
+    daily_limit = settings.deep_analysis.daily_limit
...
             week_calls = _weekly_analyzed(session, today=day)
+            day_calls = _daily_analyzed(session, today=day)
```

④ 循环内**在周配额检查之后**加日配额检查（顺序不能反：现有 `test_weekly_quota_blocks` 断言 `degraded` 含 key `quota`）：

```diff
             for i, paper in enumerate(candidates):
                 if week_calls >= weekly_limit:
                     report.blocked = len(candidates) - i
                     report.degraded["quota"] = f"周配额已满（{weekly_limit} 篇）"
                     break
+                if day_calls >= daily_limit:
+                    report.blocked = len(candidates) - i
+                    report.degraded["daily_quota"] = f"日配额已满（{daily_limit} 篇）"
+                    break
```

⑤ 成功计数处（与 `week_calls += 1` 同行）：

```diff
                 calls += 1
                 week_calls += 1
+                day_calls += 1
```

### 4.4 `src/ai_scholar/cli.py`（`analyze` docstring）

```diff
-    ★ 通道 A（意图触发，不看 Tier）+ B（Tier1 补读）；周配额 5 篇。
+    ★ 通道 A（意图触发，不看 Tier）+ B（Tier1 补读）；日配额 1 篇 / 周配额 5 篇（削峰）。
```

### 4.5 `tests/test_deep_analysis.py`

① `strong_settings` fixture 末尾加一行（把削峰在通用用例里关掉，避免影响既有多篇断言）：

```python
    settings.deep_analysis.daily_limit = 5  # 通用用例不测削峰；削峰由专门用例显式设 1
```

② 新增 2 个测试：

```python
async def test_daily_quota_smooths_peak(tmp_path: Path) -> None:
    """★ 回归（2026-11-02 周一 ¥0.3108）：单次 run 最多 daily_limit 篇，不再一天跑满整周。"""
    db = tmp_path / "d.db"
    seed(db, [make_paper(f"arXiv:A{i}") for i in range(3)])
    settings = strong_settings(str(tmp_path))
    settings.deep_analysis.daily_limit = 1
    report = await run_deep_analysis(
        settings=settings,
        db_path=db,
        client=FakeDeepLLM(),
        watchlist=WATCHLIST,
        today=DAY,
    )
    assert report.analyzed == 1
    assert report.blocked == 2
    assert "daily_quota" in report.degraded


def test_daily_quota_default_is_one() -> None:
    assert DeepAnalysisSettings().daily_limit == 1
```

（`DeepAnalysisSettings` 需加入 `ai_scholar.config` 的 import。）

### 4.6 活文档同步（★ 只改活文档，归档/历史快照不动）

| 文件 | 改什么 |
|---|---|
| `README.md` | ④ 深度分析一篇（231）· 配置表加 `deep_analysis.daily_limit` 行（287）· 「每天自动跑什么」(301) 与成本档位表 (317) 改「日 1 篇 / 周 5 篇」 |
| `CODEBUDDY.md` | 架构总览 LLM 环节（124）· 模块地图 `deep_analysis.py` 行（202）· 成本可见性段补一句根因 |
| `docs/成本预算.md` | §9.4 后补「日配额 1 篇 = 稳态表口径；11/2 周一尖峰根因与削峰」 |
| `docs/handoff/L9-开工.md` | §2 LLM 环节描述同步（当前开工入口，下个 Agent 唯一上下文） |
| `docs/文档维护日志.md` | §2 登记本卡 |
| 本卡 §7 | 实施结果 |

**明确不改**：`项目复盘归档/`（历史快照）· `docs/开发日志.md`（时间线事实）· `L2/L3/L4/L8-开工.md`（已归档）· `.github/workflows/daily.yml`（`analyze` 不传 `--limit`，自动走新配额）· `window_starts`（`spent_in_windows` 的金额闸门口径必须保持自然周，不能连带改）。

## 5. DoD（可断言）

| # | 验收 |
|---|---|
| D-1 | `DeepAnalysisSettings().daily_limit == 1`，`config/default.yaml` 与代码默认值一致 |
| D-2 | 单次 run 在 `daily_limit=1` 下 `analyzed == 1`、`blocked == 剩余`、`degraded` 含 `daily_quota` |
| D-3 | 周配额仍生效（`test_weekly_quota_blocks` 绿）、废调用仍不烧额度（E9 回归绿） |
| D-4 | 既有全部测试绿（离线全量 + `test_docs_consistency.py` 门禁） |
| D-5 | `ruff check` / `ruff format --check` / `mypy strict` 全绿 |
| D-6 | 文档四处口径一致，`test_docs_consistency.py` 锚点不漂 |
| D-7 | 本次零 API 花费；无 push / 无 tag |

## 6. 验收命令

```bash
uv run pytest tests/test_deep_analysis.py tests/test_docs_consistency.py -q
uv run pytest -m "not live" -q
uv run ruff check . && uv run ruff format --check .
uv run mypy src/ai_scholar
uv run ai-scholar cost --days 14 --json   # 零 LLM，确认本地账本未被改动
```

## 7. 实施结果（2026-11-05）

- 按 §4 diff 逐项落地，无偏离：`config.py` / `config/default.yaml` / `deep_analysis.py`
  （`_daily_analyzed` + 配置读取 + 双层闸门 + 计数递增）/ `cli.py` docstring /
  `tests/test_deep_analysis.py`（fixture 隔离 + `test_daily_quota_smooths_peak`
  + `test_daily_quota_default_is_one`）。
- 活文档同步：README 4 处 · CODEBUDDY 3 处 · `成本预算.md` §9.4 后 ·
  `L9-开工.md` §2 · `文档维护日志.md` §2。归档/历史文档按 §4.6 **未动**。
- `window_starts` 未改 ⇒ `spent_in_windows` 的金额闸门仍是自然周口径（§4.6 约束成立）。
- 验证：`pytest -m "not live"` 100% 绿 · `mypy src/ai_scholar` Success（47 files）·
  `ruff check` / `ruff format --check .` 全绿（122 files）· `test_docs_consistency.py` 9 绿。
- `ai-scholar cost --days 14 --json` 未改动本地账本（只读复核）。
- 本次**零 API 花费**；无 push / 无 tag。
