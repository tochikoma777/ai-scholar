# AI Scholar

AI 文献的 **采集 → 关联 → 推演 → 沉淀**，外加**论文深度分析**，输出自包含 Markdown 的 **Obsidian 知识库**。

> ★ **核心判据（不是收了多少篇论文）**：**导出物能不能直接喂给别的 AI 工具**。
> 因此导出物是**合成文档**（条目元信息 + why + 我的笔记），而不是条目列表。
>
> 动机："没看完的东西以后还能用"。

- 许可证：**MIT**（只管代码，不管数据 —— 数据署名见文末）
- 姊妹项目：[aipulse](https://github.com/tochikoma777/aipulse)（三档模式 / 三级降级 / 静默失败治理 全盘继承）
- 当前阶段：**L0-C**（Obsidian 导出器 + `list`）—— 已完成

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
uv run ai-scholar export    # 生成 vault（默认 20 篇）
```

`doctor` 会打出一张体检表：Python 版本 / LLM 配置 / 代理 / 磁盘 / watchlist / 各数据源连通性。
没填 key 时它显示"自动降级 off"——**这不是错误**，程序照常采集与导出，只是跳过 LLM 增强。

想启用 LLM：`cp .env .env` 后填入（或直接在 `.env` 里写）：

```bash
AI_SCHOLAR_LLM__BASE_URL=https://api.deepseek.com
AI_SCHOLAR_LLM__API_KEY=sk-...
```

运行档位三档：`off`（零花费）/ `fast`（`deepseek-chat`）/ `strong`（`deepseek-reasoner`）。

### 第一个 vault 在哪？

```bash
uv run ai-scholar export --out ~/vaults/ai-scholar   # 默认 ./vault，默认导出 20 篇
uv run ai-scholar list                              # 看库内条目（★ = watchlist 命中）
```

★ **不想先跑采集？** 直接看仓库里的 [`example_vault/`](example_vault/00-首页.md) ——
那是**示例 vault**（合成数据，frontmatter 标了 `example: true`），
演示导出物的完整形态：frontmatter / 标记块 / 用户笔记区。

- 导出目标目录由 `--out` 指定，**默认 `./vault`**。
- ★ **默认不是全量导出**（§5.6：年增 18.8 万篇会让 Obsidian 不可用）；
  用 `--limit` / `--ids` / `--since` 圈定范围。
- ★ **vault 是独立的 private 仓库**，与本项目**物理隔离**：本仓库的 CI 只会 commit public 资产，
  vault 由你本地 `export` 写入，里面是你的笔记。
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
- [ ] **L1** 关联层：Semantic Scholar 引用边（★ 只走 `POST /graph/v1/paper/batch`）
- [ ] **L2** 推演层：LLM 三档（off / fast / strong）+ 预算护栏 + 结果持久化
- [ ] **L3** 沉淀层增强：主题 MOC（`20-主题/`）+ Daily Note（`40-日志/`）+ 六段深度分析正文
      （vault 基础导出已在 **L0-C** 落地：标记块 / frontmatter 合并 / 原子写入）
- [ ] **深度分析**：六段结构 + 三句半 + 背景透镜（延迟定级，有施引数据才输出"历史地位"）

---

## 开发

```bash
uv run pytest                     # 全量测试（respx mock，不发真实网络）
uv run ruff check . && uv run ruff format --check .
uv run mypy src/ai_scholar
uv run python scripts/probe_sources.py    # 数据源探测（需联网，产出 docs/数据源手册.md）
```

数据源实测结论见 [`docs/数据源手册.md`](docs/数据源手册.md)（Agent 维护，**真实响应**摘录，
每源只存 8 KB 代表性片段）。
