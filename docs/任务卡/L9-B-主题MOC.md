# 任务卡 · L9-B 主题 MOC（`20-主题/`，watchlist 口径）

> 状态：**已完成**（2026-10-28）
> 归属阶段：L9（运营期）
> 依据：`docs/handoff/L9-开工.md` §6 候选 2 · 设计方案 §6.1（`20-主题/` 位置已预留）·
> §4.2.2（watchlist 组 = 主题词清单）

## 1. 背景（为什么做）

- vault 现在只有**论文笔记**和 `00-首页.md`，缺一个**主题维度入口**：
  "我关心的 multimodal 方向，库里到底攒了多少篇"要自己搜。
- 设计方案 §6.1 早给 `20-主题/` 留了位置，但**从未生成**。
- 主题来源**不新造**：直接用 watchlist 的 4 个组（负责人 2026-10-28 定案：用 watchlist 口径，
  不另建 `config/topics.yaml`）—— 组和权重你已经调过，语义稳定可解释。

## 2. 范围

| # | 要做什么 | DoD（可断言） |
|---|---|---|
| B-1 | 生成 `20-主题/{组名}.md` | 每个 watchlist 组一页；无命中的组**不生成**（避免空壳噪音） |
| B-2 | 页内结构 | frontmatter（`title` / `schema_version` / `topic` / `updated`=数据日期**不含生成时间**）+ 标记块内生成区：命中统计 + 论文列表（标题 + `id` + 首见日 + Tier） |
| B-3 | 链接规则 | **只链到已存在的笔记**（`papers.note_path` 存在且文件在）；未导出的论文**只列标题，不做 wikilink**（死链是最差体验） |
| B-4 | 幂等 | 连续两次 `export` 无 diff；`export --check` 覆盖主题页 |
| B-5 | 边界 | 不读 vault 内容反写 db（红线）；不渲染个人笔记；主题页不进 public 仓库（vault 是 private） |
| B-6 | 文档同步 | `CODEBUDDY.md` 数据落点表 + §6 说明；`docs/设计方案.md` §6.1 标已实现；维护日志 |

**明确不做**：不做关键词自动聚类（设计未给细节，v1.11 已定由 TF-IDF 相似替代）；
不调 LLM 给主题写描述（零 LLM 是可解释性底线）。

## 3. 关键路径

| 文件 | 职责 |
|---|---|
| `src/ai_scholar/export/obsidian.py` | 调 `render_topic_mocs()`；目录常量加 `20-主题` |
| `src/ai_scholar/export/topic.py` | **新增**：按 watchlist 组聚合库内论文 → MOC 内容（纯函数） |
| `src/ai_scholar/export/base.py` | 复用标记块 + 原子写机制（不新造） |
| `tests/test_export.py` | 新增：生成/幂等/死链不出现/无命中不生成 |

## 4. 验收命令

```bash
uv run ai-scholar export                 # 生成主题页
uv run ai-scholar export --check         # 应退出 0（连续两次无 diff）
ls ~/projects/ai-scholar-vault/20-主题/  # 应看到 4 个组（有命中的）
uv run pytest tests/test_export.py -q
```

## 5. 成本

**零 LLM、零网络**：只读 db 与 watchlist 配置。

## 6. 实现结果（2026-10-28）

- `export/obsidian.py: render_topic_moc()` + `topic_note_path()`：
  frontmatter（`title` / `topic` / `schema_version`）+ 标记块内「命中统计 · 关键词 · 有笔记列表」
  + 标记块外用户区（永不覆盖）。
- `export/base.py: TopicEntry`（`note_name is None` ⇒ **不渲染 wikilink**，死链禁令）。
- `pipeline/export.py: _topic_docs()`：按 watchlist 分组聚合；无命中不生成；
  只列**有笔记**的论文，未导出的进「未导出 N 篇」计数；排序（首见日 → id）确定 ⇒ 幂等。
- 实测（临时 vault，库内 2174 篇）：生成 4 个主题页（multimodal 命中 150 篇 / 有笔记 5 篇），
  `export --check` ⇒ **✓ 无变更（8 篇，幂等）**。
- 2 条单测（`tests/test_export.py`）：命中组生成 / 未命中组不生成 / 全文 wikilink 数 = 1（死链禁令）
  / 连续两次导出生成区逐字节一致 + 用户区保留。

**与卡片的偏差**：B-2 原写「命中统计 + 论文列表（含未导出）」—— 改为**未导出只计不列**，
否则主题页会塞进上百条点不开的条目（实测 multimodal 命中 150 篇 / 只有 5 篇有笔记）。
