# 贡献指南（CONTRIBUTING）

> 本文件只写「怎么在本仓库干活」的可执行约定；设计动机见 `docs/设计方案.md`，
> 红线与坑位见 `CODEBUDDY.md` / `docs/执行手册.md`。

---

## 1. 环境

```bash
uv sync                              # 依赖（含 dev 组）
uv run ai-scholar doctor             # 环境体检（无 LLM key 也全绿，退出码 0）
```

- Python **3.11**，包管理用 **uv**（不要用 `pip install -e .`，会绕过 `uv.lock`）。
- 未配 LLM key ⇒ 自动降级 `off` 档，全链路照跑；**不要为此报错或改代码**。

## 2. 提交前必须全绿

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy src/ai_scholar                    # strict；cli.* / utils.* 有放宽
uv run pytest -m "not live"                   # 离线全量，等价 CI 的无密钥跑法
uv run pytest tests/test_docs_consistency.py  # 改了文档/handoff 必跑
```

单条调试：

```bash
uv run pytest tests/test_export.py::test_note_name_has_date_prefix_and_short_hash
uv run pytest -k "config or watchlist"
```

## 3. 数据与仓库边界（最容易被踩）

| 规则 | 原因 |
|---|---|
| **不要提交 `data/reports` / `data/signals` / `data/predictions` / `data/first_seen.jsonl` / `data/analyses` 的本地重跑产物** | 这些是 public 真源，由 daily bot 负责提交；本地覆盖会让降级证据消失（经验固化 G5） |
| `data/ai_scholar.db` 是可丢弃缓存 | 已在 `.gitignore`，不要入库 |
| vault（`~/projects/ai-scholar-vault`）是 private 仓库 | 任何导出测试都必须用 `tmp_path`；**禁止在 Actions 日志里打印笔记内容** |
| 不要 force push；push 前先 `git rebase origin/main` | bot 每天往 main 推数据 commit |

## 4. 花钱的改动

新增或修改任何 LLM 调用点前，先给出「单次 / 日 / 月 / 年」成本估算，上线后回填实测；
`off` 档必须恒 0 且不报错。查账：`uv run ai-scholar cost --days 14`。

## 5. 文档

- `CODEBUDDY.md` = 助手工作手册（改代码前先看「改哪里 → 先跑什么」）。
- 阶段交接只在 `docs/handoff/`；稳定结论回流 `docs/设计方案.md` / `docs/执行手册.md` / `docs/经验固化.md`。
- 文档改动登记到 `docs/文档维护日志.md`。

## 6. 提交信息

沿用 Conventional Commits 前缀（`feat:` / `fix:` / `docs:` / `chore:` / `test:`），
并在括号里标阶段或模块，例如 `feat(L9-D): 报告目录分层`、`fix(collect): 逐日隔离`。
