# 任务卡 · L8-F 成本可见性（`cost` 命令 + 异常判定）

> 状态：**全部完成**（2026-10-26）—— F-1～F-4 + guard / 站点趋势 / Actions 告警
> 归属阶段：L8（运营验证与使用闭环）
> 依据：`docs/成本预算.md` §9（实测回填）· `docs/经验固化.md` E9（口径要能对上产出）

## 1. 背景（为什么做）

- 今天（2026-10-26）首轮 Actions 实测：**¥0.2361 里 80% 是废调用**，
  而报告里 `cost_cny` 一直"有数字"——没人能从中看出异常。
- 现有承载只有"当日一个数"（report json / 报告 md / 站点卡片），
  **没有累计、没有拆分、没有环比、没有告警**；`budget` 命令只是单次调用演练。
- 成本是**静默失败**：不主动翻日志就永远不知道（E5 同类）。

## 2. 范围（本期只做这两块）

| # | 要做什么 | DoD（可断言） |
|---|---|---|
| F-1 | `ai-scholar cost` 命令 | 输出 **今日 / 近 7 日 / 本月 / 本年 / 全部** 累计；`--json` 可被脚本消费；`--days N` 可调窗口 |
| F-2 | 分项拆分 + 异常判定 | 按 `purpose`（deep_analysis / grading / enhance / 其他）拆分；给出近 7 日中位数、今日环比；命中"异常"要给**明确原因** |
| F-3 | 真源口径写死在代码里 | 累计以 `data/reports/*.json` 的 `cost_cny` 为**真源**（public、可审计）；db `llm_usage` 只用于**分项**，并显式标注"缓存、可能不完整" |
| F-4 | 文档同步 | `CODEBUDDY.md` 命令区 + 文档索引；`docs/成本预算.md` 增"怎么查"；维护日志登记 |

**后续三件（同日已并入本期）**：

| # | 做什么 | DoD |
|---|---|---|
| F-5 | 报告内 **cost guard** | `DailyReport.cost_anomalies` 非空 ⇒ md 顶部 ⚠️ 段；**不进 `degraded`**（与源故障分家） |
| F-6 | 站点**成本趋势** | 内联 SVG 柱状（近 N 日）+ 本月累计 + 异常 badge；零外部请求、逐字节幂等 |
| F-7 | Actions `::warning::` | daily 增 `Cost guard` 步骤：打印今日/本月/本年 + 对每条异常打 `::warning`；**退出码 0** |

## 3. 异常判定规则（必须可解释，不许拍脑袋）

命中任一条即 `anomaly=True`，并写清是哪条：

1. `今日 > budget.daily_limit_cny × 0.5`（日限额半程告警，默认 ¥1.00）；
2. `今日 > 近 7 日（不含今日）中位数 × 3`（相对突增；中位数为 0 时跳过该条）；
3. `本月 > budget.monthly_limit_cny × 0.8`（月度接近上限）。

★ 判定只**标注**，不阻断（退出码 0）；阻断是 `check_budget` 的职责，两者不混。

## 4. 关键路径

| 文件 | 职责 |
|---|---|
| `src/ai_scholar/pipeline/cost.py` | **新增**：读 reports / 汇总 / 分项 / 异常判定（纯函数，零网络） |
| `src/ai_scholar/cli.py` | `cost` 命令（表格 + `--json`） |
| `tests/test_cost.py` | 累计边界（空目录 / 缺字段 / 坏 JSON）、中位数、异常三条规则 |
| `docs/成本预算.md` | §10「怎么查历史花费」 |

## 4.5 实现结果（2026-10-26）

- `pipeline/cost.py`：`load_report_costs`（坏文件显式返回）· `summarize_reports`（滚动窗口/月/年/全部）
  · `load_usage_by_purpose`（db 只读、失败降级为提示）· `detect_anomalies`（三条规则）。
- `cli.py: cost [--days N] [--json]`：表格 + 环比 + 分项 + 异常标注；退出码恒 0（只标注不阻断）。
- 15 条单测（`tests/test_cost.py`）：空目录 / 坏 JSON / cost=null / 非法 date / 窗口边界 /
  中位数排除今日 / 三条异常规则各自触发 / 缺 db 降级 / CLI `--json`。
- 实测：`ai-scholar cost` ⇒ 今日 ¥0.8189、覆盖 2 天报告、分项 deep_analysis 0.3647 /
  enhance_link 0.1130 / enhance 0.0537 / grading 0.0296，并提示 `2026-10-24.json: cost_cny 为 null`。

## 4.6 后续三件实现结果（2026-10-26）

- `pipeline/cost.py: live_anomalies()`：报告时刻的实时判定（今日成本来自 db、历史来自 reports，
  **不重复计今天的旧报告**）。
- `report.py`：新增 `cost_anomalies` 字段；md 模板顶部 ⚠️ 段；与 `degraded` 严格分开。
- `site.py` / `html.py`：`cost_series` / `cost_month` / `cost_anomalies` + 内联 SVG 柱状板块；
  实测站点输出 `本月累计 ¥0.8189`、SVG 存在、无外部请求、重跑逐字节一致。
- `daily.yml`：`Cost guard` 步骤（`cost --json | python` 打 `::warning::`），不阻断。
- 测试：report 3 条（异常出现/正常不出现/不进 degraded）、site 3 条（趋势/异常/幂等）、
  cost 1 条（不重复计数）；全量 **371 passed**。

## 5. 验收命令

```bash
uv run ai-scholar cost                 # 人读表格
uv run ai-scholar cost --json          # 脚本消费
uv run ai-scholar cost --days 30       # 自定义窗口
uv run pytest tests/test_cost.py -q    # 边界与规则
```

## 6. 成本

**零 LLM、零网络**：只读本地 `data/reports/*.json` 与 db（只读）。
