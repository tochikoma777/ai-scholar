# CHANGELOG

> **本文件只回答一个问题：每个 tag 交付了什么（功能 / 修复 / 接线）。**
>
> 为避免同一内容分散到不同源头，以下内容**不在此重复**，请去对应文档查：
> - 「为什么这么做、踩了什么坑」→ `docs/开发日志.md`（按阶段，记决策与教训）
> - 「各文档自身的版本号与对应 tag」→ `docs/文档维护日志.md`（文档版本台账）
> - 阶段开工 / 收尾证据 → `docs/handoff/L*-开工.md`
>
> 日期取各 tag 的创建日期（`git for-each-ref`）。

---

## [L8a] — 2026-10-26 · tag `l8a`

- **L8 收尾（运营验证与使用闭环）**
  - C：GitHub Pages 发布 —— daily job 内 `site --out _site` → `upload-pages-artifact` → `deploy-pages`
  - D-1：collect 逐日隔离 + 60s 读超时
  - B：本地 vault 物化（深度分析产物反向回填 db 再导出）
  - A：Actions 开启 LLM（job 级 secret，enhance/grade/analyze 共用）
- **L8-F 成本可见性三件套**：`ai-scholar cost` 命令 + 异常四处可见（报告 md / 报告 json `cost_anomalies` / 站点「成本」板块 / Actions `::warning`）
- **全量审计修复 LLM 三处缺陷**（commit `fbec610`）：grade 重复付费 / 判废无回显致误诊 / 连续失败无刹车；enhance 漏记账；hf 降级被盖；审计报告换行
- daily 加 `concurrency` 防并发 push 覆盖真源

## [L7a] — 2026-10-26 · tag `l7a`（含 [L7] 决策 2026-10-25 · tag `l7`）

- 接线 8 项（v1.11 决策已定，勿推翻）：
  - D 排序：热度 / 经典交替 + 去重
  - `classics` 直采（`config/classics.yaml` + `collect --classics`）
  - 深度分析产物落 `data/analyses/` 为 public 真源 + `analyze --check` + `sync_analyses` 自愈 + `export` 反向回填
  - `want` 点读队列（`data/wanted.jsonl`，报告/站点消费）
  - `links.json` 有向边；frontmatter `score` 分项（无 total）
- 关键教训固化：export 必须在选篇**之前**消费 `analyses` 与 `wanted`，否则「分析过/点读过却导不出」

## [L6] — 2026-10-25 · tag `l6`（运营修复）

- HF 400 根因修复：HF 官方 `date` 上限为昨天，采集窗口从昨天起；失败带截断响应体
- 成本可见性初版：daily 内按本地日窗口汇总 `cost_cny`
- site 透出报告自身 `degraded`（此前源报错页面仍显「正常」）

## [L5b] — 2026-10-25 · tag `l5b`（含 [L5a]）

- L5a：静态自包含 HTML 站点（只读 public 数据，不读 vault）
- L5b：内联原生 JS 交互看板（筛选 / 排序 / 搜索，无外部请求、无第三方库）

## [L4] — 2026-10-25 · tag `l4`（推演与演化）

- public 记分板 `data/predictions/scoreboard.jsonl` + **可证伪门禁（代码判定）**
- `predict` / `stats --verify` **零 LLM、零网络**；`insufficient` 不进命中率分母；样本不足不下结论

## [L3] — 2026-10-25 · tag `l3b1` / `l3b2`

- 增强 `enhance`（fast）：一句话 / 为什么重要 / 方法标签 + 关联解读
- 定级 `grade`（fast）：**延迟定级** → Tier 1/2/3 + 原始证据

## [L2] — 2026-10-25 · tag `l2c`（含 [L2] 关联）

- 关联 `relate`：文献耦合（按 `ref_external_id` 倒排分桶）+ TF-IDF 相似（增量）；对称边单存 `src<dst`
- L2-C：vault `40-日志/YYYY-MM-DD.md` Daily Note（标记块内生成区，外区永不覆盖）

## [L1] — 2026-10-24 · tag `l1a` / `l1b` / `l1c`

- 采集 `collect`：arXiv（按 `submittedDate` 翻页）+ HF Daily Papers
- 三级去重：归一化主键 / 跨源 / 跨版本（alias → canonical）
- `first_seen.jsonl` append-only（绝不覆盖已有 id）

## [L0] — 2026-10-22 · tag `l0a` / `l0b` / `l0c`

- 仓库骨架、配置层（`${ENV}` 插值、watchlist 双文件合并）、数据源探测

---

### 说明

- 早于 L4 的细分 tag（`l0c`/`l1a`–`l1c`/`l2`/`l3a`/`l2c`/`l3b1`/`l3b2` 等）按阶段归并呈现，避免与 `docs/文档维护日志.md` 的逐文档版本表重复。
- 仓库中存在一个孤立 tag `l10`（2026-10-23），其含义未在文档中记录，未纳入本表；若需补全请据 git 历史核实后补条目。
- 审计修复（`fbec610`，属 L8a 范围）的细节见 `docs/开发日志.md` 与各 `tests/test_*.py` 新增断言。
