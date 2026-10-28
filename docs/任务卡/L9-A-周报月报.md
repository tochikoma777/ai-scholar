# 任务卡 · L9-A 周报 / 月报（零 LLM 汇总）

> 状态：**已完成**（2026-10-28）
> 归属阶段：L9（运营期）
> 依据：`docs/handoff/L9-开工.md` §6 候选 1 · `docs/成本预算.md` §10（真源口径）·
> `docs/经验固化.md` E5（口径要能对上产出）

## 1. 背景（为什么做）

- 现在只有**日报**：想看"这一周怎么样"要人肉翻 7 个 `data/reports/*.json`。
- 日报是**当天快照**，天然看不到：新增趋势、Tier 1 累计、成本环比、推演命中率、降级复发。
- 周报/月报属于**纯派生**（由日报重算），**零 LLM、零网络**，是 L9 里性价比最高的一件。

## 2. 范围

| # | 要做什么 | DoD（可断言） |
|---|---|---|
| A-1 | `ai-scholar report --weekly` | 产出 `data/reports/weekly-YYYY-Www.{md,json}`；覆盖 7 个自然日（含当日） |
| A-2 | `ai-scholar report --monthly` | 产出 `data/reports/monthly-YYYY-MM.{md,json}`；按自然月窗口 |
| A-3 | 汇总口径 | 新增篇数 · 覆盖天数/缺失天 · Top 命中（按**出现次数**聚合并给 id+标题）· 新增定级（Tier 1/2/3 分布）· 深度分析篇数 · 成本（合计 + 按 purpose 分项）· 推演（hit/miss/insufficient + 命中率）· 降级（按 key 计数，标明复发次数） |
| A-4 | 幂等 | **不含生成时间**；同一份输入连续两次产出逐字节一致（与 `site` 同口径） |
| A-5 | 真源口径 | 只读 `data/reports/*.json`（public 真源）+ 记分板/观点 JSONL；**不读 vault**、不读 db |
| A-6 | 文档同步 | `CODEBUDDY.md` 命令区；`docs/执行手册.md`；维护日志登记 |

**明确不做**：不接入 `daily.yml`（L9 默认不新增自动化；要自动周发需负责人另定）；
不调 LLM 写"本周总结"（成本 + 会引入不可复现文本）。

## 3. 关键路径

| 文件 | 职责 |
|---|---|
| `src/ai_scholar/pipeline/recap.py` | **新增**：读报告真源 → 聚合 → `RecapReport` 模型 |
| `src/ai_scholar/templates/report/recap.md.j2` | **新增**：渲染（与 daily 同 jinja 配置：`trim_blocks` 注意行尾） |
| `src/ai_scholar/cli.py` | `report --weekly` / `--monthly` |
| `src/ai_scholar/models.py` 或 `pipeline/recap.py` | `RecapReport` pydantic 模型（json 同源） |
| `tests/test_recap.py` | **新增**：窗口边界 / 缺失天 / 坏 JSON 跳过 / 幂等 / 分项聚合 / CLI |

## 4. 验收命令

```bash
uv run ai-scholar report --weekly        # 生成本周周报
uv run ai-scholar report --monthly       # 生成本月月报
uv run ai-scholar report --weekly --date 2026-10-20   # 指定日所在周
uv run pytest tests/test_recap.py -q
diff <(cat data/reports/weekly-*.md) <(uv run ai-scholar report --weekly && cat data/reports/weekly-*.md)  # 幂等
```

## 5. 成本

**零 LLM、零网络**：只读本地 public 文件。

## 6. 实现结果（2026-10-28）

- `pipeline/recap.py`（新增）：`week_window` / `month_window`（自然周 = 周一~周日；自然月 = 1 号~月末）
  · `load_daily_reports`（坏 JSON / 非法日期**显式进 notes**，非日报文件名跳过）
  · `build_recap`（命中按**天数**聚合、Tier 分布、成本合计/日均/最高单日/异常天数、推演 hit/miss、
    降级**复发天数**）· `write_recap`（原子写，无生成时间）。
- `templates/report/recap.md.j2`（新增）+ `report.py: render_recap`（**新增渲染器**，§6.6 允许）。
- CLI `report --weekly` / `--monthly`（互斥；`--out` 只改派生产物落点，真源固定 `data/reports/`）。
- 实测（本地，本周 W44）：覆盖 3/7 天 · 新增 1259 篇 · 定级 55 篇 · 成本 ¥1.1299（日均 ¥0.3766，
  最高 ¥0.9709@10-26）· 降级 `arxiv` 1 天 / `classics` 1 天；重跑逐字节一致。
- 8 条单测（`tests/test_recap.py`）：窗口边界 / 天数聚合 / `insufficient` 不进分母 /
  坏 JSON 与目录缺失显式上报 / 幂等 / json roundtrip / 板块齐全且无时间戳。

**与卡片的偏差（如实记录）**：A-3 原计划「成本按 purpose 分项」—— 日报 json 里没有 purpose 字段，
分项只能读 db（违背"只读 public 真源"）。**本期不做分项**，改为「合计 / 日均 / 最高单日 / 异常天数」，
全部可由真源重算。要分项需先给 `DailyReport` 加 `cost_by_purpose` 字段（属 schema 变更，另开卡）。
