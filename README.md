# AI Scholar

AI 文献的 **采集 → 关联 → 推演 → 沉淀**，外加**论文深度分析**，输出自包含 Markdown 的 **Obsidian 知识库**。

> ★ **核心判据（不是收了多少篇论文）**：**导出物能不能直接喂给别的 AI 工具**。
> 因此导出物是**合成文档**（条目元信息 + why + 我的笔记），而不是条目列表。
>
> 动机："没看完的东西以后还能用"。

- 许可证：**MIT**（只管代码，不管数据 —— 数据署名见文末）
- 姊妹项目：[aipulse](https://github.com/tochikoma777/aipulse)（三档模式 / 三级降级 / 静默失败治理 全盘继承）
- 当前阶段：**L7 接线阶段**（自动深度分析 → 沉淀进 vault，不新增方案）+ L5/L6 已完成（HTML 看板 / 运营修复）+ L4 已完成（推演 / 记分板 / 校准 / 影响分析）

---

## ★ 诚实标注：覆盖范围

| 项 | 状态 |
|---|---|
| 英文文献源（arXiv / Hugging Face Daily Papers / Semantic Scholar） | ✅ 覆盖 |
| **中文文献源** | ❌ **目前不覆盖**（不采集中文社区源；不引入中文分词依赖） |
| 论文正文 / 全文 | ❌ **永不入库**。将来只在临时目录抓取、抓完即弃（合规红线） |

---

## 5 分钟上手

```bash
git clone https://github.com/tochikoma777/ai-scholar.git
cd ai-scholar
uv sync                    # 安装依赖（含 dev）
uv run ai-scholar init      # 建目录骨架 + 生成 config/watchlist.local.yaml + .env 骨架
uv run ai-scholar doctor    # 环境体检（★ 没配 LLM key 也全绿，自动降级 off，退出码 0）
uv run ai-scholar collect   # 采集（默认回看 7 天，见下）
uv run ai-scholar export    # 导出**已收藏**的论文（默认只导 selected；--all 导最新 N 篇）
uv run ai-scholar site      # 静态 HTML 单文件（只读报告/记分板/DB，默认 ~/projects/ai-scholar-site）
uv run ai-scholar enrich     # 补查 S2 引用（★ 全程 batch，无 key 也能跑）
uv run ai-scholar signals    # 时序采样（★ 状态持久化，按到期采样，不每日全量重算）
uv run ai-scholar relate     # 文献耦合 + TF-IDF 相似（★ 倒排分桶，零 LLM）
uv run ai-scholar enhance    # LLM 增强：一句话 / 为什么重要 / 方法标签（fast；需 key）
uv run ai-scholar grade      # 文献定级：Tier 1/2/3（fast；延迟定级）
uv run ai-scholar analyze --ids arXiv:2610.30132   # 深度分析（strong；正文临时抓取）
uv run ai-scholar predict    # 可验证推演（零 LLM）：进记分板，到期自动验证
uv run ai-scholar stats      # 记分板命中率；--calibration / --impact 看校准与影响
uv run ai-scholar mark arXiv:2610.30122      # 显式收藏（export 默认只导收藏的）
uv run ai-scholar want arXiv:2610.30122      # 想读（报告/站点点读用的就是这条：写 data/wanted.jsonl 并置位）
uv run ai-scholar related arXiv:2610.30122   # 看某篇的关联（引用了 / 被引用 两栏）
```

`doctor` 会打出一张体检表：Python 版本 / LLM 配置 / 代理 / 磁盘 / watchlist / 各数据源连通性。
没填 key 时它显示"自动降级 off"——**这不是错误**，程序照常采集与导出，只是跳过 LLM 增强。

想启用 LLM：`cp .env .env` 后填入（或直接在 `.env` 里写）：

```bash
AI_SCHOLAR_LLM__BASE_URL=https://api.deepseek.com
AI_SCHOLAR_LLM__API_KEY=sk-...
```

运行档位三档：`off`（零花费）/ `fast`（`deepseek-v4-flash`）/ `strong`（`deepseek-v4-pro`）。

### 第一个 vault 在哪？

```bash
uv run ai-scholar export --out /tmp/vault-preview   # 默认 ~/projects/ai-scholar-vault，20 篇
uv run ai-scholar export --check                    # 只比对不写盘：无变更退出 0，有变更退出 1
uv run ai-scholar list                              # 看库内条目（★ = watchlist 命中）
```

★ **不想先跑采集？** 直接看仓库里的 [`example_vault/`](example_vault/00-首页.md) ——
那是**示例 vault**（合成数据，frontmatter 标了 `example: true`），
演示导出物的完整形态：frontmatter / 标记块 / 用户笔记区。

- 导出目标目录由 `--out` 指定，**默认 `~/projects/ai-scholar-vault`**（配置项 `export.out_dir`，
  只准 `~/` 开头）；`--out` 落在代码仓库内会被**直接拒绝**（含 `example_vault/`）。
- ★ **默认不是全量导出**（§5.6：年增 18.8 万篇会让 Obsidian 不可用）；
  用 `--limit` / `--ids` / `--since` 圈定范围。
- ★ **vault 是独立的 private 仓库**，与本项目**物理隔离**：本仓库的 CI 只会 commit public 资产，
  vault 由你本地 `export` 写入，里面是你的笔记。
- ★ **vault 用 git 就不要再叠加 Obsidian Sync / iCloud** —— 两套同步会互相覆盖。
- ★ 真 vault 不在代码仓库的版本控制里，`git diff` 看不到它 ⇒ 用 `export --check` 验证幂等
  （比对范围：文献笔记 + `00-首页.md` + `links.json`；`生成报告.md` 是运行日志，允许 diff）。
- 导出物是**合成文档**：条目元信息 + why + 你的笔记；正文用 `<!-- BEGIN/END GENERATED -->` 标记块保护，
  **生成区之外的内容永不覆盖**。

---

## ★ `watchlist.local.yaml`：填你自己的方向

```bash
cp config/watchlist.yaml config/watchlist.local.yaml   # 或直接 `ai-scholar init`，它会自动复制
```

加载顺序：**`watchlist.local.yaml` 深合并覆盖 `watchlist.yaml`**（同名组整组覆盖，local 没写的组完整回落）。

```yaml
# config/watchlist.local.yaml（示例：整组覆盖 multimodal，并新增一组）
default_threshold: 0.9        # 任一命中即触发
groups:
  - name: multimodal          # 与示例同名 → 整组覆盖（weight + keywords 一起换）
    weight: 2.0
    keywords: [video understanding, spatial reasoning]
  - name: my-own-topic        # 新组，追加
    weight: 1.2
    keywords: [speculative decoding, KV cache]
```

匹配语义：

- 匹配范围是 **title 或 abstract**，word-boundary，大小写不敏感；
- 命中【布尔】：`score ≥ threshold` ⇒ **触发**（决定要不要它）；
- 命中【分】：`Σ(命中组权重)`，**同组多次命中只计一次** ⇒ **只用于排序**；
  ★ 分是排序权重，**不是门槛**（不要拿它当"更值得收录多少"的判据）。

**为什么 `watchlist.local.yaml` 不入库**：配置洁癖 —— 别人 clone 下来不该带着你的兴趣清单。
它已被 `.gitignore` 排除，且有单测断言 `git ls-files` 不含它。仓库里那份 `config/watchlist.yaml`
是**中性示例**（只含通用主题）。

---

## 数据来源与署名

本项目采集并展示来自以下来源的公开数据：

- **arXiv**：`export.arxiv.org/api/query`（arXiv 提供的公开 API，无需认证；[Terms of Use](https://info.arxiv.org/help/api/tou.html)）。
  论文元数据版权归各自作者所有。
- **Hugging Face Daily Papers**：`huggingface.co/api/daily_papers`（公开只读 API）。
- **Semantic Scholar**：`api.semanticscholar.org`（L1+ 启用，用于引用 / 施引数据）。
  ★ **Semantic Scholar 的许可要求：公开展示其数据时必须署名** —— 本段独立于 LICENSE，
  因为 **MIT 只管代码，不管数据**。
  > Data provided by Semantic Scholar (Allen Institute for AI), used under its
  > [API Terms of Use](https://www.semanticscholar.org/product/api).

★ 引用原文的限制（对齐 STM/PSP 2016 学术引用指南）：public 产物每条引文 ≤ 100 词、
全篇合计 ≤ 300 词，完整署名且不改动引文；**图表一律不复制**（改为自己重画并注明
"Adapted from Data in <作者等, 年份>"）。实验数字是事实，可自由复述。

---

## Roadmap

- [x] **L0-A** 仓库骨架 + 配置层（三档模式 / `${ENV}` 插值）+ watchlist 双文件 + 数据源探测
- [x] **L0-B** DB schema（SQLModel + Alembic）+ arXiv / HF 采集器 + 三级去重 + `first_seen`
- [x] **L0-C** Obsidian 导出器（标记块 / frontmatter 合并 / 文件名清洗 / 原子写入）+ `list`
- [x] **L1-0** vault 迁出（独立 **private** 仓库）+ 双仓库五条防护 + `export --check` + 报告秒级时间
- [x] **L1-A** S2 引用关联：`paper/batch` → 出边入库 + 补齐队列 + 双轨渲染（`enrich` / `related`）
- [x] **L1-B** signals 采样状态机 + 零成本预筛 + `papers.selected` 选集（`signals` / `mark` / `--all`）
- [x] **L1-C** 文献耦合（倒排索引）+ TF-IDF 语义相似 + 双轨渲染（`relate`）
- [x] **L2** Actions 自动化：每日报告 + JSONL 持久化 + commit 回 public（tag `l2`）
- [x] **L2-C** Daily Note（vault `40-日志/`）：本地 `export` 生成 + 用户区标记块保护 + 幂等（tag `l2c`）
- [x] **L3a** LLM 增强（fast）：一句话 / 为什么重要 / 方法标签 / 关联解读 + 预算闸门记账（tag `l3a`）
- [x] **L3b-1** 文献定级（fast）：三判据 + 六硬约束 + 延迟定级/幂等（tag `l3b1`）
- [x] **L3b-2** 深度分析（strong）：六段 + 三句半 + 背景透镜 + 正文临时抓取（tag `l3b2`）
- [x] **L4** 推演与演化：可验证推演进记分板 / 观点不计分 / 定级校准 / 影响分析（tag `l4`）
- [ ] **L5** 呈现形态
  - [ ] **L5a** 静态 HTML 单文件（零依赖、不渲染个人笔记）
  - [ ] **L5b** 交互式看板（单文件 + 原生 JS，零后端）

---

## 开发

```bash
uv run pytest                     # 全量测试（含 2 个 @pytest.mark.live 联网冒烟；其余 respx mock）
uv run pytest -m "not live"       # 离线全量（跳过联网冒烟，无网环境用这条）
uv run ruff check . && uv run ruff format --check .
uv run mypy src/ai_scholar
uv run python scripts/probe_sources.py    # 数据源探测（需联网，产出 docs/数据源手册.md）
```

数据源实测结论见 [`docs/数据源手册.md`](docs/数据源手册.md)（Agent 维护，**真实响应**摘录，
每源只存 8 KB 代表性片段）。
