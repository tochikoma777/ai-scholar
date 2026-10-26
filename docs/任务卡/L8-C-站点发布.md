# 任务卡 · L8-C 站点发布（GitHub Pages）

> 状态：**已完成**（2026-10-26）· 归属阶段：L8

## 1. 问题

`ai-scholar site` 产物只落本机目录（`~/projects/ai-scholar-site`），**别人看不到**，
"报告/看板"的价值没发挥；而站点是只读 public 数据，发布不涉及私有内容。

## 2. 方案与关键决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 发布目标 | **GitHub Pages（Actions 构建）** | 仓库已 public；零额外服务、零费用 |
| 构建位置 | **在 `daily` job 内构建** | 站点要读 db 元数据，而 db 只在 Actions 缓存（git 里没有）；独立 workflow 拿不到 |
| 部署位置 | 独立 `deploy-pages` job | `deploy-pages` 需要 `github-pages` environment + OIDC；混在 daily 里会让"部署失败"看起来像"采集失败" |
| 产物目录 | `_site/`（已 gitignore） | 构建产物不入库，避免污染真源 |

## 3. DoD

1. `daily.yml` 有 `Build site` → `Upload Pages artifact` → `deploy-pages` 三件套；
2. `permissions` 含 `pages: write` / `id-token: write`；
3. 仓库 Pages 已启用（`build_type=workflow`）；
4. 部署成功后 `https://tochikoma777.github.io/ai-scholar/` 可访问，且页面**不含**个人笔记/正文。

## 4. 结果

- Pages 已启用：`https://tochikoma777.github.io/ai-scholar/`（`build_type=workflow`）。
- `daily.yml`：`permissions` + `Build site` + `Upload Pages artifact` + `deploy-pages` job。
- `_site/` 进 `.gitignore`。
- 站点仍只读 public 数据（reports / 记分板 / db 元数据），无外部请求、逐字节幂等。

## 5. 验收

```bash
gh api repos/tochikoma777/ai-scholar/pages --jq '.html_url,.build_type'
gh run list --workflow=daily.yml --limit 1      # deploy-pages job 应为 success
```
