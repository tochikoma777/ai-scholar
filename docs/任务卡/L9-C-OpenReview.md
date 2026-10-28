# 任务卡 · L9-C OpenReview 顶会信号（先实测，后接线）

> 状态：**探测完成 · 接线待定**（2026-10-28）
> 归属阶段：L9（运营期）
> 依据：`docs/handoff/L9-开工.md` §6 候选 3 · 设计方案 §5.5（标 L1+，可选增强）·
> `docs/经验固化.md` D1（外部源能力**先实测再接线**）

## 1. 背景（为什么做）

- 顶会（ICLR/NeurIPS/ICML…）的**接收信号**比 arXiv 提交更能说明"这篇被认可"。
- 设计方案 §5.5 把它标成 L1+ 可选增强，一直没做。
- ★ 前提纪律（D1）：**没实测过的数据源不许接线**。ar5iv 的教训是——错误回退会伪装成真数据，
  比返回 `None` 更糟。所以本期第一步**只做探测**，结论决定第二步。

## 2. 范围

| # | 要做什么 | DoD（可断言） |
|---|---|---|
| C-1 | `scripts/probe_openreview.py` 实测 | 真实请求 OpenReview API（v2 `/notes/search` 或 `/venues`），产出：可达性 / HTTP 码 / 真实字段 / 限流行为 / 单会议记录数；**每源只存 8 KB 代表性片段** |
| C-2 | 结论写进 `docs/数据源手册.md` | 与既有源同格式（真实响应摘录）；不可达 ⇒ 写"不可达 + 原因"，**不伪造 fixture** |
| C-3 | 接线（**仅当 C-1 通过**） | 新增 collector + `base.py` 源常量；定位为**信号源**（只补 `openreview_venue` 等字段），**不产生新条目**（对齐 HF 的定位，§2.1.1） |
| C-4 | 合规与成本 | 标注 API 使用条款/速率限制；无 key 时优雅跳过（`degraded` 只 warning，退出码 0） |
| C-5 | 文档同步 | `CODEBUDDY.md` 环境分级表 + 数据源表；维护日志 |

**退出条件（不丢脸的失败）**：C-1 实测不可达 / 需要付费 key / 条款不允许批量抓取 ⇒
**只交付探测结论并停止**，把决策交回负责人。这本身就是合格交付（D1）。

## 3. 关键路径

| 文件 | 职责 |
|---|---|
| `scripts/probe_openreview.py` | **新增**：探测脚本（与 `probe_s2.py` 同风格，向终端打印实测结论） |
| `docs/数据源手册.md` | 新增 OpenReview 章节 |
| `src/ai_scholar/collectors/openreview.py` | （条件性）**新增**：只查已入库论文的会议/接收状态 |
| `tests/test_openreview.py` | 解析用 respx 拦截真实摘录；不可达时 `pytest.skip(reason=...)` |

## 4. 验收命令

```bash
uv run python scripts/probe_openreview.py      # 联网实测
uv run pytest tests/test_openreview.py -q      # 有真实摘录才断言，否则 skip
```

## 5. 成本

探测阶段：**零 LLM**，只有少量 HTTP。接线阶段必须给出：预估请求量 + `off` 档行为 + `degraded` 文案。

## 6. 实测结果（2026-10-28，本地 WSL）

| 项 | 实测 |
|---|---|
| 可达性 | ✅ `api2.openreview.net/notes/search` 全部 HTTP 200（`api.openreview.net` v1 也通；`api2/venues` 404） |
| 可用字段 | `content.venue.value` / `content.venueid.value` / `invitations` / `content.title.value` |
| 抽样 20 篇最近论文 | 请求 21 次 · 耗时 44.6s（限速 1 req/2s） |
| 严格命中（标题相似度 ≥ 0.9） | **8 篇（40%）** |
| venue 分布 | `ICLR 2027 Conference Submission` 5 · `ACL ARR 2026 October Submission` 2 · `AdaptFM Poster` 1 |
| 误命中 | 存在：如「Uniform convergence at optimal rate and scale」→「Optimal PAC Bounds without Uniform Convergence」（相似度 0.42） |

## 7. 结论：**本期只交付探测，不接线**

理由（按优先级）：

1. **拿到的不是"接收"信号，是"投稿"信号**。实测里 7/8 的 venue 带 `Submission` 字样
   （投稿中），与设计 §5.5 想要的"顶会接收"不是一回事；要区分需再探 decision/venue 状态机。
2. **误命中会把假数据写进笔记**。相似度 0.6~0.9 区间实测存在同领域不同论文，
   写进 frontmatter 就是 D1 说的"伪装成真数据"，比不写更糟。
3. **没有批量接口**：`notes/search` 只能按 term 逐篇查，1 req/2s ⇒ 全量 800 篇/天 ≈ 27 分钟/天；
   只能限定子集（selected / Tier 1），那就需要先定"给谁查"。
4. **没有消费端**：即使查到，也要先决定落在哪（db 新列 + 迁移 / public 真源 JSONL）、
   谁读它（笔记 frontmatter / 站点 / 定级输入）。没定消费端就接线 = E7「能力已实现≠已接线」。

**若要开工（建议作为独立任务卡 L10）**，先把这四件事定下来：
① 只查哪些论文（建议：watchlist 命中 ∪ Tier 1，≤30 篇/天）
② 命中判据（相似度阈值 + venue 白名单，如只要 `ICLR|NeurIPS|ICML|ACL|CVPR` 的**正式 venue**）
③ 落点（建议 `papers.openreview_venue` + 迁移，不新增 public 真源）
④ 消费端（笔记 frontmatter `venue_or` + 站点筛选）
