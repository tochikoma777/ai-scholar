# CODEBUDDY.md

This file provides guidance to CodeBuddy Code when working with code in this repository.

> 本文件指导 CodeBuddy 在本仓库中工作。
> ★ 项目负责人提供初稿，Agent 后续维护（可补充命令、目录、阶段状态）。
> ★ 若本文件与当前任务卡冲突，**以任务卡为准**。
> ★ 全程中文回复（技术术语 / 代码 / 路径除外）。

---

## ★ 当前阶段：L2（Actions 自动化 + 每日报告）—— 已完成（tag `l2`）

- **L2-前置**：模型名迁移（`deepseek-v4-flash`/`v4-pro`）+ 预算闸门（`budget.py`：
  `estimate_cost` + `check_budget`，四层上限 + 单次 run 次数）。
- **L2-A**：每日报告产物（`pipeline/report.py` + `templates/report/daily.md.j2` + `report` 命令），
  产出 `data/reports/YYYY-MM-DD.{md,json}`。
- **L2-B**：`daily.yml`（cron UTC 16:30 + workflow_dispatch）：collect → enrich → signals → relate → report，
  真源（first_seen / signals / 报告）commit 回 public，db 走 actions-cache 跨天续命。
- ★ **L2-C Daily Note**（vault `40-日志/`）**推迟到 L3 之后** —— 依赖深度分析/新定级数据，届时再做。

上一阶段：L1-C（文献耦合 + TF-IDF 相似）。
下一阶段（**未开始**）：L3a（LLM 增强 fast：一句话 / 为什么重要 / 方法标签 / 关联解读）。

★ L3a 开工前必读：
- LLM 客户端层**尚未实现**（无 build_client / NullLLM / 三级降级）—— 从零实现，httpx 直连，**不用 openai SDK**。
- `llm_usage` 记账闭环（`cost_cny` / `cache_hit` 字段 + migration）待办在 `budget.py` docstring。
- 预算闸门已就绪（L2 前置），L3 调用前接上；off 档必须零请求。

当前阶段以任务卡为准（文档维护分级见下方「文档归属」）。

---

## 项目身份

`ai-scholar`：AI 文献的**采集 → 关联 → 推演 → 沉淀**，外加**论文深度分析**
（六段结构 + 三句半 + 背景透镜），输出自包含 Markdown 的 **Obsidian 知识库**。

| 项 | 值 |
|---|---|
| 核心判据 | ★ **导出物能不能直接喂给别的 AI 工具**（不是收了多少篇） |
| 导出物形态 | **合成文档** = 条目元信息 + why + 我的笔记（**不是条目列表**） |
| 许可证 | MIT |
| 仓库 | GitHub **public** |
| LLM 默认 | DeepSeek：`deepseek-chat`（fast）/ `deepseek-reasoner`（strong） |
| 运行模式 | 三档 `off` / `fast` / `strong`；**未配 key 自动降级 off 且不报错** |
| 本地路径 | `~/projects/ai-scholar`（WSL2 原生 ext4，**不要放 `/mnt/c/`**） |

---

## 架构总览（★ 跨文件才看得懂的部分）

```
cli.py (typer)
├─ collect ─→ pipeline/collect.py ─┬─ collectors/arxiv.py ─┐
│                                 └─ collectors/hf.py ────┴→ RawPaper
│                                    └→ store/hash.py · first_seen.py · models.py
└─ export/list ─→ pipeline/export.py ─→ export/obsidian.py（db → Markdown）

横切层：config.py（YAML + ${ENV} + watchlist 合并） · utils/http.py（唯一网络出口）
```

分层规则（违反就会重新引入已踩过的坑）：

- **collectors 只产出 `RawPaper`**，不碰 DB、不碰 watchlist。新增数据源 = 一个 collector
  函数 + `base.py` 里的源常量，去重与入库逻辑不用动。
- **pipeline 是唯一的编排层**：事务、单源隔离、汇总报告都在这里。collector 抛的异常
  在此转成 `degraded`，不得外泄成非零退出码。
- **export 是单向物化**：db → Markdown。禁止从 vault 反读并回写 db。
- **一切 HTTP 走 `utils/http.py:HttpClient.request`**（令牌桶限速 / 重试 / UA / 代理）。
  测试靠 respx 拦截；off 档断言"0 请求"依赖这一唯一出口。
- **配置只从 `config/*.yaml` + `.env` 读**，代码内不硬编码阈值；环境开关必须 `${ENV}`。

### 两条主线的关键路径

**collect** — `pipeline/collect.py:259 run_collect`

1. `_days_back(days)` 逐日抓取（arXiv 按 `submittedDate` 精确框定并翻页；HF 按 `date`）。
2. `canonical_id_for` → `_resolve_via_alias` → `upsert_paper`。三级去重：
   ① 归一化主键（arXiv id 剥版本号 / DOI 小写）
   ② 跨源（HF 只补 `hf_score`，不覆盖元数据、不新增条目）
   ③ 跨版本（`paper_aliases` 指向 canonical，别名不产生新条目）。
3. 新条目先 `first_seen.append`（先查后写，已有跳过）→ **db 里的 `first_seen_at` 只是缓存**。
4. 每次采集写一行 `runs`，降级原因进 `degraded_json`。

**export** — `pipeline/export.py:96 run_export`

1. `select_papers` 圈定范围（`--limit` / `--ids` / `--since`，**默认 20 篇，不是全量**）。
2. `ObsidianExporter.render`：读旧文件 → frontmatter 读-合并-写回（`USER_KEYS` / `OBSERVED_KEYS`）
   → 生成区内容不变则保留 `content_rev_at`。
3. `Exporter.write` 原子写（`.tmp` + `os.replace`），避免 Obsidian 读到半截文件。
4. 收尾写 `00-首页.md` / `90-元数据/生成报告.md` / `90-元数据/links.json`。

### 模块地图

| 路径 | 职责 | 关键点 |
|---|---|---|
| `cli.py` | 十个子命令：`init`/`doctor`/`collect`/`export`/`enrich`/`mark`/`signals`/`relate`/`related`/`list` | 退出码：正常恒 0、`ConfigError` 1、参数错误 2 |
| `config.py` | YAML 加载、`${ENV}` 插值、watchlist 双文件合并、`Settings` | `load_settings` 带 `lru_cache`；环境变量前缀 `AI_SCHOLAR_`，嵌套分隔 `__` |
| `models.py` | pydantic 域模型：`WatchlistConfig` / `Availability` / `TimeSemantics` | **与 `store/models.py` 不是一回事** |
| `collectors/base.py` | `RawPaper` 统一中间表示 | 只有元数据，**没有正文字段** |
| `collectors/s2.py` | S2 `paper/batch` 客户端 | ★ 未命中返回 `null`（位置对齐）；超限降半重试；无 key 不发 `x-api-key` |
| `pipeline/collect.py` | 编排、三级去重、upsert、`runs` | 源码级禁止 import watchlist |
| `pipeline/enrich.py` | 引用补齐：S2 → 出边 + 队列状态机 | ★ 方向语义固定为 src 引用 dst；入库前先写方向单测 |
| `pipeline/prescreen.py` | 零成本预筛（规则负面排除） | ★ 只决定是否跟踪；`r` 是淘汰集留存率 |
| `pipeline/signals.py` | 采样状态机 + signals 写入 + 月度 JSONL | ★ 状态持久化；两套时钟不可混用 |
| `pipeline/relate.py` | 文献耦合（倒排）+ TF-IDF 相似（增量） | ★ 禁止全库两两比较；对称边只存一条 |
| `store/hash.py` | 主键归一化 + `content_hash` | hash 只含 title/abstract/authors/venue |
| `store/first_seen.py` | `first_seen.jsonl` 读写 | append-only，绝不覆盖已有 id |
| `store/models.py` | 六张 SQLModel 表 | 时间字段一律 TEXT ISO-8601 UTC |
| `export/base.py` | `Exporter` ABC + 原子写 | 不为"将来"加 Registry / 插件机制 |
| `export/obsidian.py` | 标记块、frontmatter 合并、双轨链接渲染 | 标记块注释里**不能有时间戳** |
| `utils/http.py` | 限速 / 重试 / 代理 / UA | 只重试 408/425/429/5xx 与超时 |
| `utils/timeutil.py` | UTC 工具 + 三态时间语义 | 代码内取"现在"只用 `utcnow()` |

### 配置与路径解析

- `project_root()` = 仓库根，可被 `AI_SCHOLAR_ROOT` 覆盖（测试用它指向 `tmp_path`）。
- 数据路径全部由 `Settings` 派生：`data_dir()` / `db_path()`；`first_seen.jsonl` 在 `data_dir()` 下。
- `export.out_dir` 默认 `~/projects/ai-scholar-vault`（`expanduser`）；`--out` 的相对路径按
  仓库根解析，且最终必须落在仓库**之外**，否则抛 `UnsafeOutputPath`（含 `example_vault/`）。
- `load_settings()` 先 `load_dotenv(override=False)` 再插值：不这么做，写进 `.env` 的 key 读不到
  （`Settings(**yaml)` 的 init 参数优先级高于 `env_file`）。
- watchlist 合并：`watchlist.local.yaml` 的**同名组整组覆盖** `watchlist.yaml`，不做逐字段合并。

---

## ★ 数据落点（判断"数据在哪"先看这张表）

| 数据 | 可再生？ | 落点 | 入库？ |
|---|---|---|---|
| 论文元数据、关联边、引用列表 | ✅ 重采即回 | `data/ai_scholar.db`（**可丢弃缓存**） | ✗ |
| **`first_seen_at`**（首次见到它的日期） | ❌ | `data/first_seen.jsonl` | **✓ public** |
| **signals 时序快照**（被引增长曲线） | ❌ | `data/signals/YYYY-MM.jsonl` | **✓ public** |
| 每日报告 md + json | ✅ | `data/reports/YYYY-MM-DD.{md,json}` | **✓ public** |
| 用户笔记 | ❌ | **独立 private vault 仓库**：`~/projects/ai-scholar-vault/` | ✗（不进本仓库） |
| 论文正文 / 全文 | — | **永不入库**（临时抓取、抓完即弃） | ❌ 绝对禁止 |
| `config/watchlist.local.yaml` | — | 本地（配置洁癖：不该让别人 clone 到你的兴趣清单） | ✗ |

★ **vault 与代码仓库物理隔离**。本项目的 Actions **只 commit public 仓库**；
vault 由本地 `export` 写入。任何"让 Actions 写 private vault"的设计都已被否掉，不要提出。

---

## 开工前必读

> **off 档跑得再久也覆盖不了 fast / strong 路径。** 涉及 LLM 的改动，
> 本地桩测试全绿不等于真实可用 —— 必须配 key 实跑一次才算验证过。

> **禁止凭印象写字段。** 一切解析必须基于 `docs/数据源手册.md` 里的真实响应。
> 拿不到真实响应就 `pytest.skip(reason=...)` 并写明原因，**绝不伪造 fixtures**。

---

## 文档归属（★ 同一份文件只能一方写）

> 2026-10-24 起**全部由 Agent 维护**，按变更性质分级（详见 `docs/经验固化.md`）：
> **L1 事实同步**（状态/命令/tag 栏/错别字）与 **L2 决策记录** → Agent 直改；
> **L3 意图变更**（改核心判据/红线/阶段划分/数据落点/成本上限）→ Agent 出提案 + diff，**负责人批准**。

| 文档 | 维护方 |
|---|---|
| `docs/设计方案.md` · `docs/执行手册.md` · `docs/文档维护日志.md` | **Agent**（分级维护，L3 变更需负责人批准） |
| `docs/数据源手册.md` · `docs/开发日志.md` | **Agent** |
| `docs/成本预算.md` · `docs/经验固化.md` · `docs/handoff/*.md` | **Agent** |
| `README.md` · `CHANGELOG.md` · `CONTRIBUTING.md` · `CODEBUDDY.md` · `pyproject.toml` | **Agent** |

★ **先提交代码 + 打 tag，再刷新文档**（否则维护日志的 tag 栏对不上）。

---

## 项目特有红线（★ 这些是本项目独有的，不是通用工程规范）

1. ❌ **把论文正文 / 全文写入仓库任何位置** —— 合规红线。将来抓取只在临时目录，
   `persist` 永远 false，**抓完即弃**。若有人提议"存下来省得重复抓"，回绝。
2. ❌ **`content_hash` 里包含引用数 / 热度 / score** ——
   hash 是"这是不是同一篇"的**身份指纹**，只由 `title + abstract + authors + venue` 计算
   （作者字符串需先归一化：小写、去标点、`姓+名首字母`）。
   引用数等**观测值照存**，但存进 `signals` 表按天一行，**不进 hash**。
   否则每天 hash 都变 → 重复入库 / 重复定级 / `first_seen` 被覆盖 → 成本随天数线性增长。
3. ❌ **把 `[[wiki-link]]` 当成 PDF 下载** —— 那是 Obsidian 双链，指向 vault 笔记。
   vault 笔记只含元数据 + 生成区 + 用户笔记，**不含全文**。
4. ❌ **引用原文超限长 / 复制图表** —— public 产物引用原文**每条 ≤ 100 词、
   全篇合计 ≤ 300 词**（对齐 STM/PSP 2016 学术引用指南，Elsevier / Wiley / Springer /
   OUP / ACS 等出版商共同签署），必须完整署名、**不得改动引文**，
   且只引用为说明观点所必需的最小片段。
   vault 属私人学习笔记，可放宽，但仍禁止整段摘录。
   ★ **图表一律不复制** —— 复制图表几乎总是需要许可，这是比文字严重得多的雷区；
   改为自己重画并注明 "Adapted from Data in <作者等, 年份>"。
   ★ **事实不受版权保护**：结论、数字、实验结果用自己的话复述即可，
   **优先改写而非引用**——这既能避开版权，也符合 C1"导出物是合成文档"的定位。
   ★ **数字自由**：BLEU / FLOPs / 加速比 / 参数量 / 准确率等**实验数字是事实，
   不是表达**，可自由引用且不受上述词数限制**（三句半的【疗效】字段正是靠它成立）。
   引用时仍须注明出处章节，便于你自己回查。
5. ❌ **逐篇 GET 调 Semantic Scholar** —— 必须 `POST /graph/v1/paper/batch`，
   一次最多 500 个 id，`fields` 走 URL query 参数，Header `x-api-key`。
6. ❌ **覆盖用户笔记** —— vault 正文必须用标记块 `<!-- BEGIN/END GENERATED -->` 保护，
   生成区之外的用户区**永不覆盖、永不重做**。
7. ❌ **导出产生 diff 噪音** —— 文件内**只留内容级时间戳**（why/note 内容变了才更新）；
   导出时间移出文件，只进 `90-元数据/生成报告.md`。hash 基于**每条目的 why/note 内容**，
   不是"条目集合"。
8. ❌ **Tier 当门槛用** —— **Tier 是排序权重，不是门槛**。
   深度分析通道 A（watchlist 命中 / 显式指定 / 当日 Top N）**立刻执行、不看 Tier**；
   正文抓取跟随深度分析触发，不按 Tier 限流。
9. ❌ **新论文入库即定级** —— 延迟定级。三条判据里的**结构权重需要施引数据**，
   新论文没有；入库只做零成本预筛，等引用数据补齐（或已被顶会接收）才定级。
10. ❌ **让 LLM 编造历史地位 / 影响力** —— 六段之五「历史地位」是**条件式输出**：
    有施引数据才输出并附证据；没有就留空并写「待观察：发表 N 天，被引 M 次」。
    prompt 明令禁止以"可能 / 有望"填充。
11. ❌ **输出单一综合评分** —— 客观信号与 LLM 判断**分列**，由人自己权衡。
12. ❌ **SOTA / first / breakthrough 等论文自述当作定级证据** —— 硬约束。
13. ❌ **把 watchlist 当入口过滤器** —— watchlist **只做三件事**：① 打 ★ ② 触发通道 A
    深度分析 ③ 参与排序。**永远不决定论文是否入库**。
    领域过滤 = cs.AI/CL/LG，**已在 arXiv 查询的 `cat:` 里施加过**，不要再滤第二遍。
    > ⚠️ **本项目已犯两次同类层次混淆**（另一次是把定级硬约束搬进 `exclude_penalty`）：
    > 把上层（意图 / 定级）规则搬到下层（采集 / 入口）。`N_enter` 会随
    > `watchlist.local.yaml` 漂移而**不可复现**，且关联图会只剩我关心的方向 ——
    > 直接背叛动机「没看完的东西以后还能用」。
    > 守这条的测试：`test_pipeline_never_imports_watchlist`（源码级断言
    > 采集编排不得引用 watchlist）。
14. ❌ **关联方向搞反 / 写库外目标的边** ——
    `paper_links(src,dst,'references')` 的语义固定为 **src 引用了 dst**；
    入边**不单独存储**（`idx_links_dst` 反向查询即得）；
    被引目标不在 `papers` 表 ⇒ **不写边**，只累加 `refs_count`。
    ★ 方向建模错误能通过所有测试、只是结果反了 —— 必须先写方向单测（出边 / 入边 / 互为反向）。
15. ❌ **回采被引论文** —— 每篇引约 30 条 × 515 篇/天 ⇒ ≈1.5 万条/天（**564 万行/年**），库会失控。
16. ★ **不得因"关联边很少"判定 L1-A 失败** —— 冷启动期引用关联**必然稀疏**
    （§5.1b：新论文引用的是库外老论文）；真正的冷启动主线是 L1-C 的语义相似。
17. ❌ **采样状态每日重算** —— 状态必须持久化（`signal_state` / `signal_next_at`）。
    新论文只在当天属于"新采集"，每日重算 ⇒ 只有 1 个快照 ⇒ C8 延迟定级被架空。
18. ❌ **dormant 复活条件里带 age** —— 采样点 age 恒 ≥180 ⇒ 全量复活。
    条件**只**是 `citation_count ≥ min_citations`，且**最多一次**。
19. ❌ **两个时钟混用** —— 成熟度 = `published_at`（快路径 / 复活点）；观测窗口 = `first_seen`（90 天退出）。
20. ❌ **预筛决定入库 / 把 `r` 当总体放行率** —— 预筛只决定"是否进 pending"；
    `r=0.2` 是**淘汰集**抽样留存率（总量 = `N×p + N×(1-p)×r`）。
21. ❌ **导出默认"最新 N 篇"** —— 默认只导 `selected=1`，否则每天 20 篇 = 7,300 篇/年（目标 300–500）。
22. ❌ **关联计算做全库两两比较** —— 文献耦合必须按 `ref_external_id` 倒排分桶，
    计算量 = `Σ C(n_bucket, 2)`；23 万篇两两 = 264 亿对，直接崩。
23. ❌ **`paper_references` 落 public / 存被引论文元数据** —— 只落 db（可丢弃缓存）、**只存外部 ID**
    （`ARXIV:` / `DOI:` / `S2:`）。它是文献耦合的原料，不是知识库资产。
24. ❌ **把对称边当引用渲染** —— `similarity` / `bibcoupling` 必须走独立分节（相似 / 文献耦合），
    不能混进「引用了」；对称边只存一条（`src < dst`），渲染时双向展开。

---

## 继承 aipulse 的工程红线

- ❌ 引入 `openai` SDK —— httpx 直连（自带 httpx 2.x 冲突且导致 respx 无法 mock）
- ❌ 用 `ubuntu-latest` —— 固定 `ubuntu-24.04`
- ❌ 用 `datetime.now()` —— 用 `timeutil.utcnow()`；代码内一律 UTC，仅渲染层转 `Asia/Shanghai`
- ❌ 单源异常导致整条流水线崩溃 —— 必须隔离，退出码不受影响
- ❌ 把环境开关写成 yaml 字面量 —— `${ENV}` 占位符，否则被字面量压掉环境变量
- ❌ 未配 key 就报错退出 —— 自动降级 off，退出码 0
- ❌ 一次性生成整个项目 —— 分阶段，一次只做一件事
- ❌ 增强 / 定级结果每次重算 —— **必须持久化**，同一条目不重复生成
- ❌ 时间语义只写两态 —— 必须 **event / state / discovery** 三态；
  arXiv 与 HF **索引滞后数天**，必须用 `discovery`（`first_seen`）过滤，不能用 `published_at`
- ❌ 改数据规模后不回归 —— 改了论文量相关参数，必须回头检查依赖上下文长度的环节
- ❌ `git status --porcelain` 判断能否 pull —— 只看已跟踪改动，未跟踪文件会永久阻断自动化

---

## 常用命令（随阶段扩充）

```bash
uv sync                          # 安装依赖（含 dev）
uv run ai-scholar --help         # 命令树
uv run ai-scholar init           # 目录骨架 + watchlist.local.yaml + .env 骨架
uv run ai-scholar doctor         # 环境体检（无 key 时提示降级 off，退出码 0）
uv run ai-scholar collect [--days N] [--source arxiv|hf_daily] [--dry-run]
                                 # 采集（默认回看 sources.arxiv.lookback_days=7 天）
uv run ai-scholar export [--limit N] [--ids a,b] [--since DATE] [--out PATH] [--all]
                                 # 导出 vault（★ 默认只导 selected=1；--all 导最新 N 篇）
uv run ai-scholar export --check # 只比对不落盘：无变更退出 0，有变更退出 1（幂等验收）
uv run ai-scholar export --check --all
                                 # 校验**全量笔记**（磁盘上所有 .md，不只本次选集）
uv run ai-scholar mark <paper-id> [--unmark]   # 显式收藏（决定是否生成笔记）
uv run ai-scholar signals [--limit N] [--force] [--dry-run]
                                 # 按时序状态机采样（signals 表 + data/signals/YYYY-MM.jsonl）
uv run ai-scholar relate [--type bibcoupling|similarity|all] [--limit N] [--dry-run]
                                 # 文献耦合（倒排索引）+ TF-IDF 相似（增量）；零 LLM
uv run ai-scholar enrich [--limit N] [--force] [--dry-run]
                                 # 补查 S2 引用（★ 全程 batch，禁止逐篇 GET）
uv run ai-scholar related <paper-id>
                                 # 查看关联（分「引用了」「被引用」两栏，验证方向）
uv run python scripts/probe_s2.py --n 50     # S2 实测（产出 docs/数据源手册.md §2.6）
uv run ai-scholar list [--limit N] [--since DATE] [--watchlist-only]
                                 # ★ = watchlist 命中（只做展示筛选）
uv run ai-scholar report [--date YYYY-MM-DD] [--out DIR]
                                 # 每日报告（★ 只读，写 data/reports/YYYY-MM-DD.{md,json}）
uv run ai-scholar budget --input-tokens N --max-tokens M [--tier fast|strong]
                                 # 预算演练（★ 不调 LLM，只估算成本 + 过闸门）
uv run alembic upgrade head      # 建库（★ 空 data/ 目录也能跑）

uv run python scripts/probe_sources.py        # 数据源探测（需联网，产出 docs/数据源手册.md）

uv run pytest                    # 全量（respx mock，不打真实网络）
uv run pytest tests/test_export.py -q          # 单文件
uv run pytest tests/test_export.py::test_note_name_has_date_prefix_and_short_hash -q   # 单条
uv run pytest -k "config or watchlist" -q      # 按关键字筛
uv run ruff check . && uv run ruff format --check .
uv run mypy src/ai_scholar       # strict，cli.* 与 utils.* 有放宽
```

---

## 改哪里 → 先跑什么

| 改动位置 | 先跑 |
|---|---|
| `config.py` · `config/*.yaml` | `pytest -k config`（★ 断言"实际收到的值"，不是"加载成功"） |
| watchlist 加载 | `pytest -k watchlist` + `git ls-files \| grep watchlist.local`（必须为空） |
| `utils/http.py` | `pytest -k http`（★ 断言实际发出的请求数；off 档为 0） |
| `collectors/s2.py` · `pipeline/enrich.py` | `pytest -k enrich`（★ 方向三组 + 降半重试 + 队列状态机） |
| `pipeline/signals.py` · `pipeline/prescreen.py` | `pytest -k signals`（★ 跨天/跨月时序 + 幂等） + `pytest -k prescreen` |
| `pipeline/relate.py` | `pytest -k relate`（★ 倒排分桶 / 阈值 / 增量 / 对称边不混入引用） |
| `scripts/probe_sources.py` | 实跑一次 + 检查 `docs/数据源手册.md`；不可达时确认是 skip 不是伪造 |
| `cli.py` | `pytest -k cli` + `uv run ai-scholar --help` |
| `store/hash.py`（content_hash） | `pytest -k content_hash`（★ 三条硬约束） |
| `store/first_seen.py` | `pytest -k first_seen`（append-only / 幂等） |
| `pipeline/collect.py` | `pytest -k collect` + 实跑 `ai-scholar collect --days 2` |
| `export/obsidian.py` | `pytest -k export`（★ 连续两次导出无 diff，报告除外） |
| `pipeline/export.py`（路径校验 / `--check`） | `pytest -k export` + 实跑 `ai-scholar export --check`（应退出 0） |
| `cli.py` 的 `export`/`list` | `pytest -k cli` + 实跑一次看 vault |
| **任何涉及 LLM 路径的改动** | 上面全绿也不算数 —— 配 key 实跑一次 |

---

## 测试约定

- `pytest-asyncio` 是 `asyncio_mode = "auto"`：async 测试**不需要**加 `@pytest.mark.asyncio`。
- `tests/conftest.py` 的两个 autouse fixture 是测试可复现的前提：
  清 `load_settings` 缓存、把 `load_dotenv` 打桩并 `chdir(tmp_path)`。
  **不要绕过它们**，否则本地有 `.env` 时会出现"本地失败、CI 通过"。
- 网络一律 respx 拦截；`test_live_arxiv_parse` / `test_live_hf_parse_may_skip` 是联网冒烟，
  失败/跳过不代表 CI 红。
- 断言要落在**实际收到的值**上（如 `settings.llm.base_url`），不是"加载成功"。
- CI：`ubuntu-24.04` 固定，`lint-and-test` **不读任何 secrets**；`probe` 仅在
  `workflow_dispatch` 下实跑数据源探测，不回写仓库。

---

## 环境分级

| 源 | 可用性 | 说明 |
|---|---|---|
| arXiv API | `always` | 限速约 1 req / 3s |
| HF Daily Papers | `always` | ★ availability 描述"源本身是否公开可用"，**不是**"从某台机器能不能访问"；本地 WSL 连不上官方域名是**网络环境问题**，配 `hf_base_url`（镜像）即可解，不可达只 warning + 跳过、**不计失败** |
| Semantic Scholar（L1+） | `always` | 只走 `POST /graph/v1/paper/batch` |
| OpenReview（L1，可选增强） | **`optional_enrich`** | ★ 国内访问不稳定是**已知预期**；失败只写 warning、字段留空，**不影响退出码**，不作为 bug 排查 |

★ 本地（WSL）**可联网**，探测与采集均可在本地完成；Actions 是自动化通道，不是唯一验证途径。

---

## 提交

- Conventional Commits；每个阶段结束打 tag（`l0a` / `l0b` / ...）
- ★ 提交前核对：`git ls-files | grep -E '\.env|\.db$|watchlist\.local'` 必须为空
- ★ 禁止 `git clean -fd`
