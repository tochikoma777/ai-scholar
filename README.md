# AI Scholar

[![ci](https://github.com/tochikoma777/ai-scholar/actions/workflows/ci.yml/badge.svg)](https://github.com/tochikoma777/ai-scholar/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Board](https://img.shields.io/badge/board-GitHub%20Pages-181717?logo=githubpages)](https://tochikoma777.github.io/ai-scholar/)

每天新出的 AI 论文有几百篇。收藏不等于读过，链接不等于资产。

AI Scholar 做的是第三件事：**采集 → 关联 → 推演 → 沉淀**，把论文收成一个自包含 Markdown 的
**Obsidian 知识库**，外加每日报告与静态看板。

> ★ **核心判据（不是收了多少篇论文）**：**导出物能不能直接喂给别的 AI 工具**。
> 所以导出物是**合成文档**（条目元信息 + why + 我的笔记），而不是条目列表。
>
> 动机很简单：**没看完的东西，以后还能用。**

- 在线看板：**https://tochikoma777.github.io/ai-scholar/**（每日自动发布，只含 public 数据）
- 许可证：**MIT**（只管代码，不管数据 —— 数据署名见文末）
- 姊妹项目：[aipulse](https://github.com/tochikoma777/aipulse)（三档模式 / 三级降级 / 静默失败治理，全盘继承）

---

## 目录

- [它产出什么](#它产出什么)
- [快速开始](#快速开始)
- [流水线](#流水线一条主线四个环节)
- [命令速查](#命令速查)
- [四种典型用法](#四种典型用法)
- [导出的 vault](#导出的-vault)
- [配置](#配置)
- [每天自动跑什么](#每天自动跑什么)
- [成本](#成本)
- [诚实标注：覆盖范围](#诚实标注覆盖范围)
- [数据来源与署名](#数据来源与署名)
- [开发](#开发)
- [Roadmap](#roadmap)
- [许可证](#许可证)

---

## 它产出什么

| 产物 | 落点 | 谁生成 | 公开？ |
|---|---|---|---|
| **Obsidian 知识库** | `~/projects/ai-scholar-vault`（独立 private 仓库） | 本地 `export` | ❌ 你的私人笔记 |
| **每日报告** | `data/reports/YYYY-MM-DD.md` + `.json` | Actions 每天跑 | ✅ |
| **静态看板** | `~/projects/ai-scholar-site/index.html`（单文件） | `site` 命令 | ✅（内容只取 public 数据） |
| **深度分析** | `data/analyses/{paper_id}.md` | `analyze`（strong 档） | ✅ |
| **记分板** | `data/predictions/scoreboard.jsonl` | `predict`（零 LLM） | ✅ |

★ 想先看形态再决定要不要跑？直接翻仓库里的 [`example_vault/`](example_vault/00-首页.md)：
那是**示例 vault**（合成数据，frontmatter 标了 `example: true`），演示导出物的完整形态。

---

## 快速开始

需要 Python 3.11+ 与 [uv](https://docs.astral.sh/uv/)。**没有 LLM key 也能跑完整条链路**
（自动降级到 `off` 档，跳过 LLM 增强，其余功能一个不少）。

```bash
git clone https://github.com/tochikoma777/ai-scholar.git
cd ai-scholar

uv sync                       # 安装依赖（含 dev）

uv run ai-scholar init        # 建目录骨架 + 生成 config/watchlist.local.yaml + .env 骨架
uv run ai-scholar doctor      # 环境体检（★ 没配 key 也全绿，退出码 0）
uv run ai-scholar collect     # 采集（默认回看 7 天）
uv run ai-scholar mark arXiv:2610.30122   # 收藏一篇（export 默认只导出收藏的）
uv run ai-scholar export      # 导出到 vault
```

`doctor` 打出的体检表长这样（真实输出，本地 WSL）：

```text
┏━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ 检查项          ┃ 状态    ┃ 说明                                         ┃
┡━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ Python 版本     │ OK      │ 3.11.17（要求 ≥ 3.11）                       │
│ LLM 配置        │ OK      │ 档位 fast（fast=deepseek-v4-flash）          │
│ 代理            │ 未设置  │ 直连                                         │
│ 数据目录        │ OK      │ ~/projects/ai-scholar/data · 可用 928.1 GB   │
│ watchlist       │ OK      │ watchlist.local.yaml · 4 组 / 17 关键词      │
│ vault           │ OK      │ ~/projects/ai-scholar-vault · remote: origin │
│ 数据源 arxiv    │ OK      │ HTTP 200 · 3583 B                            │
│ 数据源 hf_daily │ warning │ 不可达（ConnectTimeout）                     │
└─────────────────┴─────────┴──────────────────────────────────────────────┘
```

> `hf_daily` 那条 warning 是**本地网络环境问题**（WSL 连不上 huggingface.co），
> 不是程序故障：不可达只 warning + 跳过，**不计失败、不影响退出码**。
> 想解就配一个镜像基址（`AI_SCHOLAR_SOURCES__HF_DAILY__HF_BASE_URL`）。

想启用 LLM，在 `.env` 里写两行：

```bash
AI_SCHOLAR_LLM__BASE_URL=https://api.deepseek.com
AI_SCHOLAR_LLM__API_KEY=sk-...
```

运行档位三档：`off`（零花费）/ `fast`（`deepseek-v4-flash`）/ `strong`（`deepseek-v4-pro`）。

---

## 流水线（一条主线，四个环节）

```
        arXiv ─┐
  HF Daily ────┼─→ 采集 ─→ 三级去重 ─→ SQLite ─→ 关联 ─→ LLM 增强 ─→ 沉淀
        S2 ────┘            ↑                    │         │           │
                      first_seen.jsonl     文献耦合+TF-IDF  定级      vault / 报告 / 看板
                                          （零 LLM）        深度分析         ↑
                                                                        推演（零 LLM）
```

| 环节 | 做什么 | 花不花钱 |
|---|---|---|
| **采集** | arXiv（`cs.AI`/`cs.CL`/`cs.LG`，回看 7 天）+ HF Daily Papers + 经典清单 | 零 |
| **关联** | S2 引用边（batch）+ 文献耦合（倒排索引）+ TF-IDF 语义相似 | 零 |
| **增强** | 一句话 / 为什么重要 / 方法标签 / 关联解读（fast）、文献定级 Tier 1-3（fast）、深度分析（strong） | LLM |
| **沉淀** | vault 笔记、每日报告、静态看板、可验证推演进记分板 | 零 |

★ 三条设计上的硬规矩，理解了才好改代码：

1. **单源失败只降级，不中断** —— 某天/某源挂了记 `degraded`，其余照跑，退出码不变。
2. **增强结果必须持久化** —— 同一条目不重复生成，重跑不产生新 diff。
3. **一切 HTTP 走同一个出口**（`utils/http.py`）—— 限速、重试、UA、`off` 档的"0 请求"断言全靠它。

---

## 命令速查

全部 20 个子命令（`uv run ai-scholar --help` 看完整帮助）：

**采集与入库**

| 命令 | 作用 |
|---|---|
| `collect [--days N] [--source arxiv\|hf_daily] [--dry-run]` | 采集（默认回看 `sources.arxiv.lookback_days=7` 天） |
| `collect --classics` | 直采 `config/classics.yaml` 的经典种子清单 |
| `enrich [--limit N]` | 补查 S2 引用（全程 batch，无 key 也能跑） |
| `signals [--limit N]` | 时序采样（状态持久化，按到期采样，不每日全量重算） |
| `relate [--type bibcoupling\|similarity\|all]` | 文献耦合 + TF-IDF 相似（零 LLM） |

**LLM 增强**

| 命令 | 作用 |
|---|---|
| `enhance [--limit N]` | 一句话 / 为什么重要 / 方法标签 + 关联解读（fast，默认 20 篇） |
| `grade [--limit N]` | 文献定级 Tier 1/2/3（fast，默认 8 篇，延迟定级 + 幂等） |
| `analyze [--ids a,b] [--limit N] [--check]` | 深度分析（strong）：三句半 + 六段 + 背景透镜 |
| `budget --input-tokens N --max-tokens M` | 预算演练（★ 不调 LLM，只估算 + 过闸门） |

**推演与统计**

| 命令 | 作用 |
|---|---|
| `predict [--limit N] [--insight TEXT]` | 生成可验证推演（零 LLM），写记分板 |
| `stats [--verify] [--calibration] [--impact]` | 命中率 / 定级校准 / 影响分析；`insufficient` 不进分母 |

**沉淀与展示**

| 命令 | 作用 |
|---|---|
| `mark <id> [--unmark]` | 显式收藏（决定是否为它生成笔记） |
| `want <id>` | 标记「想读」：写 `data/wanted.jsonl` + 置位，下次 `export` 自动出笔记 |
| `export [--limit N] [--ids a,b] [--since D] [--all] [--out P]` | 导出 vault（★ 默认只导收藏的，上限 20） |
| `export --check` | 只比对不落盘：无变更退出 0，有变更退出 1 |
| `site [--out DIR] [--days N] [--limit N]` | 生成静态 HTML 单文件看板（筛选 / 排序 / 搜索，零后端） |
| `report [--date YYYY-MM-DD]` | 生成当日报告快照（只读 DB + JSONL） |
| `report --weekly` / `--monthly` | 周报 / 月报：由 `data/reports/*.json` 真源重算（零 LLM，幂等） |

**查阅与运维**

| 命令 | 作用 |
|---|---|
| `list [--limit N] [--watchlist-only] [--score]` | 列出库内论文（★ = watchlist 命中；`--score` 看分项分） |
| `related <id>` | 看某篇的关联（分「引用了」「被引用」两栏） |
| `cost [--days N] [--json]` | 查账：日 / 周 / 月 / 年累计 + 分项 + 异常（零 LLM） |
| `init` / `doctor` | 初始化环境 / 环境体检 |

---

## 四种典型用法

**① 每天早上 5 分钟**（在 Actions 自动跑完之后）

```bash
gh run list --workflow=daily.yml --limit 3     # 昨天跑成功了吗
uv run ai-scholar cost --days 7                # 花了多少钱
uv run ai-scholar export                       # 把新收藏的论文物化进 vault
```

**② 圈定自己的方向**：`config/watchlist.local.yaml`

```bash
cp config/watchlist.yaml config/watchlist.local.yaml   # 或直接 ai-scholar init
```

```yaml
default_threshold: 0.9
groups:
  - name: multimodal        # 与示例同名 → 整组覆盖（weight + keywords 一起换）
    weight: 2.0
    keywords: [video understanding, spatial reasoning]
  - name: my-own-topic      # 新组，追加
    weight: 1.2
    keywords: [speculative decoding, KV cache]
```

- 加载顺序：**`watchlist.local.yaml` 覆盖 `watchlist.yaml`**（同名组**整组覆盖**，不是逐字段合并；
  local 没写的组完整回落；`default_threshold` 单字段覆盖）。
- 匹配范围是 **title 或 abstract**，word-boundary，大小写不敏感。
- 命中【布尔】：`score ≥ threshold` ⇒ 触发（决定要不要它）。
- 命中【分】：`Σ(命中组权重)`，**同组多次命中只计一次** ⇒ **只用于排序**。
  ★ 分是排序权重，**不是门槛**，也不是"更值得收录多少"的判据。
- ★ **watchlist 永远不决定论文是否入库**（入库看领域过滤 `cs.AI/CL/LG`）。
- ★ 为什么 `watchlist.local.yaml` 不入库：别人 clone 下来不该带着你的兴趣清单
  （已被 `.gitignore` 排除，且有单测断言 `git ls-files` 不含它）。

**③ 想读某篇**：报告里的「★ 想读」区块给的是可复制命令，复制了在本地跑即可 ——
写进 `data/wanted.jsonl` 并置位，下次 `export` 自动出笔记（幂等，重复标记无副作用）。

```bash
uv run ai-scholar want arXiv:2610.32770
```

**④ 深度分析一篇**（strong 档，最贵的操作，周配额 5 篇）

```bash
uv run ai-scholar analyze --ids arXiv:2610.30132
```

产出「三句半 + 六段结构 + 背景透镜五问」：

- **三句半**（≤400 字）：坐标（开山 / 演进 / 跨界替代 / 缝合）→ 痛点 → 手术（把 A 换成 B）→ 疗效（一个数字）→ 代价。
- **六段**：总体介绍 / 核心思想 / 技术路线与结构特点 / 主要贡献 / **历史地位（条件式）** / 局限与不足。
- **背景透镜五问**：默认芯片 / 硬件视角（可换 `generic` / `backend`），回答"瓶颈资源是什么、把什么换成了什么、代价落在哪个环节"。

---

## 导出的 vault

```
vault/
├── 00-首页.md                # 概览 + 最近导出（生成区受标记块保护）
├── 10-文献/2026/2026-10-02__Paper-Title.md
├── 20-主题/multimodal.md      # 主题 MOC（按 watchlist 分组；只链已有笔记）
├── 40-日志/2026-10-26.md     # Daily Note（★ 我的笔记摘要 + 待办 在标记块外）
└── 90-元数据/
    ├── links.json            # 关联有向边（src/dst）
    └── 生成报告.md           # 导出运行日志（允许 diff）
```

每篇笔记是一份**合成文档**：frontmatter（元信息）+ 生成区（一句话 / 为什么重要 / 方法标签 / 关联文献）
+ **我的笔记**（你的，永不覆盖）。生成区由 `<!-- BEGIN/END GENERATED -->` 标记块包住，
**标记块之外的内容永不覆盖**。

★ 使用 vault 的四条边界（踩过才写在这里）：

- 导出目录由 `--out` 指定，**默认 `~/projects/ai-scholar-vault`**（配置项 `export.out_dir`，
  只准 `~/` 开头）；`--out` 落在代码仓库内会被**直接拒绝**（含 `example_vault/`）。
- ★ **默认不是全量导出**：年增约 18.8 万篇会让 Obsidian 不可用 —— 用 `--limit` / `--ids` / `--since` 圈定范围。
- ★ **vault 是独立的 private 仓库**，与本项目物理隔离：本仓库 CI 只 commit public 资产，
  vault 由你本地 `export` 写入。真 vault 不在本仓库的版本控制里，`git diff` 看不到它
  ⇒ 用 `export --check` 验证幂等（比对范围：文献笔记 + `00-首页.md` + `links.json`）。
- ★ **用 git 就不要再叠加 Obsidian Sync / iCloud** —— 两套同步会互相覆盖。

---

## 配置

配置只在 `config/*.yaml` + `.env` 里，代码不硬编码阈值；凡是环境开关一律 `${ENV}` 占位符。

| 项 | 默认 | 说明 |
|---|---|---|
| `mode` | `fast` | 三档 `off` / `fast` / `strong`；**缺 key 自动降级 `off` 且不报错** |
| `llm.fast_model` / `strong_model` | `deepseek-v4-flash` / `deepseek-v4-pro` | 换供应商 = 改 `base_url` + 加一行价格，不改代码 |
| `budget.daily/weekly/monthly/yearly_limit_cny` | 2 / 10 / 40 / 200 | 四层金额闸门，调用前拦截，**只降级不报错** |
| `budget.max_calls_per_run` | 100 | 单次 run 的调用次数上限 |
| `collect.arxiv_max_results` | 600 | 单日实测 465 条，留余量 |
| `sources.arxiv.lookback_days` | 7 | arXiv 索引滞后 3–7 天，只看当天会系统性漏采 |
| `grade.default_limit` | 8 | 日成本天天乘 365，这个值直接决定年成本 |
| `deep_analysis.weekly_limit` | 5 | 每周最多 5 篇 strong 分析；超限进队列，**不降单篇质量** |

---

## 每天自动跑什么

`.github/workflows/daily.yml`，每天 UTC 16:30（北京 00:30）：

```
migrate → collect → collect --classics → enrich → signals → relate
→ enhance → grade → analyze → analyze --check → predict → stats --verify → report
→ Cost guard → site --out _site → upload-pages-artifact → commit → deploy-pages
```

- LLM 环节只有三个：`enhance`（fast, 20）· `grade`（fast, 8）· `analyze`（strong，周 5 篇上限）。
- 末尾只 commit 五类 public 真源：`data/first_seen.jsonl`、`data/signals/`、`data/reports/`、
  `data/predictions/`、`data/analyses/`。**db 不入库**（Actions 走缓存，本地那份被 gitignore）。
- 站点在 daily job 内构建 —— 它要读 db 元数据，而 db 只在 Actions 缓存里。
- ★ **两份 db**：Actions 用缓存里的，本地是 `data/ai_scholar.db`。
  所以会出现「`data/analyses/` 里有产物，本地 vault 却没有」—— 那篇不在本地 db，
  本地补跑一次 `collect` 再 `export` 即可，**不是 bug**。

---

## 成本

| 档位 | 行为 | 成本 |
|---|---|---|
| `off` | 跳过所有 LLM 环节，其余照跑 | **恒 ¥0** |
| `fast` | 增强 + 定级 | 见下 |
| `strong` | 追加深度分析（周 5 篇上限） | 见下 |

稳态实测（2026-10-26，Actions 开 LLM）：

| 项 | 日 | 月 | 年 |
|---|---|---|---|
| `analyze`（1 篇/天） | ¥0.05 | ¥1.5 | ¥18 |
| `enhance`（20 篇 + 10 条关联解读） | ¥0.02 | ¥0.6 | ¥7 |
| `grade`（8 篇） | ¥0.073 | ¥2.2 | ¥27 |
| **合计** | **≈¥0.14** | **≈¥4.3** | **≈¥52** |

- 查账：`uv run ai-scholar cost --days 14`（零 LLM）。
- 动手改"每天必跑"的 LLM 参数前，先算 `日 × 365`：**日差 ¥0.1 = 年差 ≈¥36**。
- 成本异常会同时出现在四处（**只标注，绝不中断流水线**）：报告 md 顶部 ⚠️、
  报告 json 的 `cost_anomalies`、站点「成本」板块、Actions 的 `::warning::`。

---

## 诚实标注：覆盖范围

| 项 | 状态 |
|---|---|
| 英文文献源（arXiv / Hugging Face Daily Papers / Semantic Scholar） | ✅ 覆盖 |
| **中文文献源** | ❌ **目前不覆盖**（不采集中文社区源；不引入中文分词依赖） |
| 论文正文 / 全文 | ❌ **永不入库**。只在临时目录抓取、抓完即弃（合规红线） |

---

## 数据来源与署名

本项目采集并展示来自以下来源的公开数据：

- **arXiv**：`export.arxiv.org/api/query`（arXiv 提供的公开 API，无需认证；
  [Terms of Use](https://info.arxiv.org/help/api/tou.html)）。论文元数据版权归各自作者所有。
- **Hugging Face Daily Papers**：`huggingface.co/api/daily_papers`（公开只读 API）。
- **Semantic Scholar**：`api.semanticscholar.org`（用于引用 / 施引数据）。
  ★ **Semantic Scholar 的许可要求：公开展示其数据时必须署名** —— 本段独立于 LICENSE，
  因为 **MIT 只管代码，不管数据**。
  > Data provided by Semantic Scholar (Allen Institute for AI), used under its
  > [API Terms of Use](https://www.semanticscholar.org/product/api).

★ 引用原文的限制（对齐 STM/PSP 2016 学术引用指南）：public 产物每条引文 ≤ 100 词、
全篇合计 ≤ 300 词，完整署名且不改动引文；**图表一律不复制**（改为自己重画并注明
"Adapted from Data in <作者等, 年份>"）。实验数字是事实，可自由复述。

---

## 开发

```bash
uv run pytest                        # 全量（含 2 个 @pytest.mark.live 联网冒烟；其余 respx mock）
uv run pytest -m "not live"          # 离线全量（无网环境用这条；基线 382 passed）
uv run ruff check . && uv run ruff format --check .
uv run mypy src/ai_scholar           # strict
uv run python scripts/probe_sources.py   # 数据源探测（需联网，产出 docs/数据源手册.md）
```

- 单条 / 单文件：`uv run pytest tests/test_export.py::test_xxx -q`、`uv run pytest -k "config or watchlist" -q`。
- 联网冒烟拿不到真实响应就 `skip`，**绝不伪造 fixtures**。
- 数据源实测结论见 [`docs/数据源手册.md`](docs/数据源手册.md)（真实响应摘录，每源只存 8 KB 代表性片段）。
- 更细的架构红线、数据落点、坑位清单见 [`CODEBUDDY.md`](CODEBUDDY.md)。

---

## Roadmap

- [x] **L0** 仓库骨架 / DB schema（SQLModel + Alembic）/ Obsidian 导出器
- [x] **L1** vault 迁出（独立 private 仓库）· S2 引用关联 · signals 采样状态机 · 文献耦合 + TF-IDF 相似
- [x] **L2** Actions 自动化（每日报告 + commit 回 public）· **L2-C** Daily Note（`40-日志/`）
- [x] **L3a** LLM 增强（fast）· **L3b-1** 文献定级（fast）· **L3b-2** 深度分析（strong）
- [x] **L4** 推演与演化：可验证推演进记分板 / 观点不计分 / 定级校准 / 影响分析
- [x] **L5a** 静态 HTML 单文件 · **L5b** 交互式看板（单文件 + 原生 JS，零后端）
- [x] **L6** 运营修复：HF 400 根因 + 成本可见性 + 端到端实测
- [x] **L7** 接线：自动深度分析 → 置位 → public 真源 → 本地物化进 vault
- [x] **L8** 运营验证：Actions 开 LLM（稳态 ¥0.14/天）+ 真实 vault 物化 + Pages 发布 + collect 加固 + 成本可见性
- [ ] **L9** 运营期：持续观察与按需维护，**默认不新增功能**

下一阶段候选（需负责人定案）：OpenReview 顶会信号（已实测，接线待定）· 成本分项进日报 · S2 付费 key。
已完成（2026-10-28）：周报 / 月报 · 主题 MOC（`20-主题/`）。

---

## 许可证

代码：**MIT**（见 [LICENSE](LICENSE)）。

数据署名见上方「数据来源与署名」—— MIT 只管代码，不管数据。
