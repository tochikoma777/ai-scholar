# 任务卡 · L9-D 周报月报自动化 + 报告目录分层 + 成本折线

> 状态：**已完成**（2026-10-28）
> 归属阶段：L9（运营期，负责人 2026-10-28 定案"周报月报要自动化"）
> 依据：`docs/任务卡/L9-A-周报月报.md`（生成器已交付）· `docs/成本预算.md` §10（真源口径）·
> 负责人提案（`data/reports` 分目录）

## 1. 背景

- L9-A 只交付了**手动**生成器（`report --weekly/--monthly`）。负责人要求自动化：
  **每周一次周报、每月一次月报**。
- 同时提出 `data/reports` 多层级管理（日报/周报/月报各一个文件夹）。
- 成本可见性已有柱状图（L8-F），负责人希望**折线**以便看趋势、发现异常点。

## 2. 两个必须修正的设计点（负责人方案的两处调整）

| # | 原提案 | 问题 | 采纳方案 |
|---|---|---|---|
| 1 | 自然周/月**第一天**生成当期报告 | W44 在周一才开始 ⇒ 只会生成**空表** | 生成**上一周期**（周一→上周 W-1；1 号→上月），CLI 加 `--previous` |
| 2 | 只增加 weekly/monthly 子目录 | `data/reports/*.json` 是**成本累计真源**（`cost`/`site`/`recap` 三处各扫一遍）；只加子目录不改读取器 ⇒ 成本静默归零 | 三层 `daily/ weekly/ monthly/`；**读取器统一走 `report_paths.iter_daily_jsons`**，兼容旧平铺路径，迁移前后 `cost --json` 必须逐字节一致 |

## 3. 范围与 DoD

| # | 要做什么 | DoD（可断言） |
|---|---|---|
| D-1 | 报告目录分层 | `data/reports/{daily,weekly,monthly}/`；`daily/` 优先、旧平铺兼容、同日期不重复计数 |
| D-2 | 读取器统一 | `cost` / `site` / `recap` 全部走 `report_paths`；迁移前后 `ai-scholar cost --json` **完全一致** |
| D-3 | CLI 默认落点 | `report` → `daily/`；`--weekly` → `weekly/`；`--monthly` → `monthly/`；`--out` 仍可覆盖 |
| D-4 | `--previous` | 周一→上周、1 号→上月（当期未结束不生成） |
| D-5 | 自动化 | `.github/workflows/recap.yml`：周一 / 每月 1 号 UTC 16:35，`concurrency` 与 daily **共用**一组 |
| D-6 | 成本折线 | 站点成本板块：柱（每日）+ **折线（7 日均线）** + 异常日红点；无异常时**不画红点**；仍逐字节幂等 |
| D-7 | 测试 | 路径布局 4 条 · `--previous` 1 条 · 折线 2 条 |

**明确不做**：不改日报自身字段；不做成本分项（需要 `DailyReport` 加字段，另开卡）。

## 4. 实现结果（2026-10-28）

- 新增 `src/ai_scholar/report_paths.py`：`daily_dir/weekly_dir/monthly_dir` + `iter_daily_jsons`
  （先读 `daily/` 再读根目录，同日期以 `daily/` 为准；`weekly-*`/`monthly-*`/随便的 json 不误扫）。
- `cost.load_report_costs` / `site.load_reports` / `recap.load_daily_reports` 全部改用它；
  删除 `cost.reports_dir` 与 `site._DATE_FILE`（死代码）。
- CLI：默认落点分层；新增 `--previous`（复用 `recap.previous_period`）。
- `.github/workflows/recap.yml`：双 cron + `workflow_dispatch`（可选 weekly/monthly、
  是否 previous）；`concurrency: ai-scholar-daily`；只 `git add data/reports/{weekly,monthly}`；
  `AI_SCHOLAR_MODE=off` 且**不注入 LLM 凭据**。
- 站点成本板块：`SiteCostPoint.anomaly` + `<polyline>` 7 日均线 + 异常日 `<circle>` 红点；
  文案标注「柱=每日 / 折线=7 日均线 / 红点=异常日」。
- 数据迁移：`git mv data/reports/*.{md,json} data/reports/daily/`；
  **迁移前后 `cost --json` diff = 空**（¥1.1299 不变）。

## 5. 验收命令

```bash
uv run pytest tests/test_report_paths.py tests/test_recap.py tests/test_site.py -q
uv run ai-scholar cost --json            # 应与迁移前完全一致
uv run ai-scholar report --weekly --previous --out /tmp/x   # 生成 W-1
uv run ai-scholar site --out /tmp/site && grep -c "<polyline" /tmp/site/index.html
```

## 6. 成本

**零 LLM、零网络**。自动化 workflow 显式 `MODE=off`，顺带每天验证一次"off 档也能出报告"。
