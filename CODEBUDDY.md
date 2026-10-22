# CODEBUDDY.md

> 本文件指导 CodeBuddy 在本仓库中工作。
> ★ 项目负责人提供初稿，Agent 后续维护（可补充命令、目录、阶段状态）。
> ★ 若本文件与当前任务卡冲突，**以任务卡为准**。
> ★ 全程中文回复（技术术语 / 代码 / 路径除外）。

---

## ★ 当前阶段：L0-B（DB schema + 采集器 + 三级去重 + first_seen）—— 已完成

已完成：`data/ai_scholar.db` 六张表（alembic 迁移）、arXiv + HF 采集器、
三级去重（身份 upsert / 跨源只补 hf_score / 别名归一）、`data/first_seen.jsonl`。
`collect` 真实跑通；`export` / `list` **仍为刻意占位**（属 L0-C）。

`docs/设计方案.md` 与 `docs/执行手册.md` 由项目负责人维护，**Agent 只读**；
当前阶段以任务卡为准。

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

## ★ 数据落点（判断"数据在哪"先看这张表）

| 数据 | 可再生？ | 落点 | 入库？ |
|---|---|---|---|
| 论文元数据、关联边 | ✅ 重采即回 | `data/ai_scholar.db`（**可丢弃缓存**） | ✗ |
| **`first_seen_at`**（首次见到它的日期） | ❌ | `data/first_seen.jsonl` | **✓ public** |
| **signals 时序快照**（被引增长曲线） | ❌ | `data/signals/YYYY-MM.jsonl` | **✓ public** |
| 每日报告 md + json | ✅ | `data/reports/YYYY-MM-DD.{md,json}` | **✓ public** |
| 用户笔记 | ❌ | **独立 private vault 仓库** | ✗（不进本仓库） |
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

| 文档 | 维护方 |
|---|---|
| `docs/设计方案.md` · `docs/执行手册.md` · `docs/文档维护日志.md` | **项目负责人，Agent 只读** |
| `docs/数据源手册.md` · `docs/开发日志.md` · `docs/踩坑实录.md` | **Agent** |
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
uv run ai-scholar collect --days 2 [--source arxiv|hf_daily] [--dry-run]
                                 # 采集（★ 单源失败只 warning，退出码 0）
uv run alembic upgrade head      # 建库（★ 空 data/ 目录也能跑）

uv run python scripts/probe_sources.py        # 数据源探测（需联网，产出 docs/数据源手册.md）

uv run pytest                    # 全量
uv run pytest -k "config or watchlist" -q     # 按关键字筛
uv run ruff check . && uv run ruff format --check .
uv run mypy src/ai_scholar
```

---

## 改哪里 → 先跑什么

| 改动位置 | 先跑 |
|---|---|
| `config.py` · `config/*.yaml` | `pytest -k config`（★ 断言"实际收到的值"，不是"加载成功"） |
| watchlist 加载 | `pytest -k watchlist` + `git ls-files \| grep watchlist.local`（必须为空） |
| `utils/http.py` | `pytest -k http` + `pytest -k respx`（off 档断言 0 请求） |
| `scripts/probe_sources.py` | 实跑一次 + 检查 `docs/数据源手册.md`；不可达时确认是 skip 不是伪造 |
| `cli.py` | `pytest -k cli` + `uv run ai-scholar --help` |
| `store/hash.py`（content_hash） | `pytest -k content_hash`（★ 三条硬约束） |
| `store/first_seen.py` | `pytest -k first_seen`（append-only / 幂等） |
| `pipeline/collect.py` | `pytest -k collect` + 实跑 `ai-scholar collect --days 2` |
| **任何涉及 LLM 路径的改动** | 上面全绿也不算数 —— 配 key 实跑一次 |

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
