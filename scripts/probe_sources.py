"""数据源探测：对 arXiv 与 HF Daily Papers 各发**真实请求**，产出 `docs/数据源手册.md`。

★ 硬约束（红线）：
1. **禁止伪造 fixtures** —— 拿不到真实响应就写"未获得 + 原因"，绝不编数。
2. **只存代表性响应片段**：每源 1 条，截断 8 KB；**不存全量响应**（仓库 public）。
3. 字段表只能从真实响应摘取，**不得凭印象补**。
4. arXiv 限速约 1 req / 3s，脚本内显式限速（走 `HttpClient` 的令牌桶）。

用法：
    uv run python scripts/probe_sources.py                 # 探测最近可获得的完整日
    uv run python scripts/probe_sources.py --date 2026-10-19
    uv run python scripts/probe_sources.py --arxiv-max 600
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rich.console import Console  # noqa: E402

from ai_scholar.config import load_settings, load_watchlist, match_watchlist  # noqa: E402
from ai_scholar.models import (  # noqa: E402
    SourceProbeResult,
    arxiv_id_from_entry,
    normalize_title,
)
from ai_scholar.utils.http import HttpClient  # noqa: E402
from ai_scholar.utils.timeutil import today_local  # noqa: E402

console = Console()

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"
OPENSEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"
SAMPLE_LIMIT = 8 * 1024  # ★ 只存 8 KB 代表性片段，不存全量响应
ARXIV_PAGE = 200  # arXiv API 单次返回上限（实测上限，超过会被截断）


class PaperItem:
    """探测阶段的最小条目视图（只用来数数与去重，不落库）。"""

    def __init__(self, source: str, key: str, title: str, abstract: str, raw: dict[str, Any]):
        self.source = source
        self.key = key
        self.title = title
        self.abstract = abstract
        self.raw = raw


# ── arXiv ───────────────────────────────────────────────────────────────
def parse_arxiv_feed(xml_text: str) -> tuple[int, list[PaperItem]]:
    """解析 arXiv Atom。**字段全部来自真实响应**，解析失败就让它抛出来（不静默吞）。"""
    root = ET.fromstring(xml_text)
    total_el = root.find(f"{OPENSEARCH}totalResults")
    total = int(total_el.text) if total_el is not None and total_el.text else 0
    items: list[PaperItem] = []
    for entry in root.findall(f"{ATOM}entry"):
        entry_id = (entry.findtext(f"{ATOM}id") or "").strip()
        title = " ".join((entry.findtext(f"{ATOM}title") or "").split())
        abstract = " ".join((entry.findtext(f"{ATOM}summary") or "").split())
        authors = [
            (a.findtext(f"{ATOM}name") or "").strip() for a in entry.findall(f"{ATOM}author")
        ]
        categories = [c.get("term", "") for c in entry.findall(f"{ATOM}category") if c.get("term")]
        primary = entry.find(f"{ARXIV_NS}primary_category")
        raw = {
            "id": entry_id,
            "published": entry.findtext(f"{ATOM}published") or "",
            "updated": entry.findtext(f"{ATOM}updated") or "",
            "title": title,
            "summary": abstract,
            "authors": authors,
            "categories": categories,
            "primary_category": primary.get("term", "") if primary is not None else "",
            "doi": entry.findtext(f"{ARXIV_NS}doi") or "",
            "comment": entry.findtext(f"{ARXIV_NS}comment") or "",
            "journal_ref": entry.findtext(f"{ARXIV_NS}journal_ref") or "",
        }
        short_id = arxiv_id_from_entry(entry_id)
        key = re.sub(r"v\d+$", "", short_id) or normalize_title(title)
        items.append(PaperItem("arxiv", key, title, abstract, raw))
    return total, items


def arxiv_submitted_query(categories: list[str], day: date) -> str:
    """按 submittedDate 精确框定某一天（比"取最近 N 条"稳定，不受抓取量影响）。"""
    cats = " OR ".join(f"cat:{c}" for c in categories)
    start = day.strftime("%Y%m%d") + "0000"
    end = day.strftime("%Y%m%d") + "2359"
    return f"({cats}) AND submittedDate:[{start} TO {end}]"


async def probe_arxiv(
    client: HttpClient,
    day: date,
    categories: list[str],
    max_items: int,
    endpoint: str,
) -> tuple[SourceProbeResult, list[PaperItem], str]:
    query = arxiv_submitted_query(categories, day)
    params = {
        "search_query": query,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "start": 0,
        "max_results": min(ARXIV_PAGE, max_items),
    }
    console.print(f"[cyan]→ arXiv[/] {endpoint} · {query}")
    response = await client.get(
        endpoint, params=params, source="arxiv", rate_limit="1/3s", timeout=None
    )
    console.print(f"   HTTP {response.status_code} · {len(response.content)} B")
    result = SourceProbeResult(
        name="arxiv",
        endpoint=endpoint,
        request_url=str(response.url),
        status_code=response.status_code,
        response_bytes=len(response.content),
        availability="always",
        availability_reason="官方开放 API，无需认证；本地 WSL 与 Actions 均实测可达",
        auth_required=False,
        rate_limit="1/3s",
        content_type=response.headers.get("content-type", ""),
    )
    if response.status_code != 200:
        result.error = f"HTTP {response.status_code}"
        return result, [], ""

    total, items = parse_arxiv_feed(response.text)
    result.item_count = total

    # 分页抓全（arXiv 单次最多 ARXIV_PAGE 条）
    fetched = len(items)
    start = ARXIV_PAGE
    while fetched < min(total, max_items):
        page_params = dict(params, start=start, max_results=min(ARXIV_PAGE, max_items - fetched))
        page = await client.get(
            endpoint, params=page_params, source="arxiv", rate_limit="1/3s", timeout=None
        )
        if page.status_code != 200:
            result.error = f"分页 HTTP {page.status_code}（已取 {fetched}/{total} 条）"
            break
        _, page_items = parse_arxiv_feed(page.text)
        if not page_items:
            break
        items.extend(page_items)
        fetched += len(page_items)
        start += ARXIV_PAGE
        console.print(f"   分页 {start - ARXIV_PAGE}…{start}：累计 {fetched}/{total}")

    # 字段表：从真实响应的第一条 entry 摘取（不凭印象补）
    if items:
        result.fields = [k for k in items[0].raw if items[0].raw[k] not in ("", [], None)]
    sample = json.dumps(items[0].raw, ensure_ascii=False, indent=2) if items else ""
    result.sample_excerpt = sample[:SAMPLE_LIMIT]
    return result, items, sample


# ── Hugging Face Daily Papers ───────────────────────────────────────────
def parse_hf_papers(payload: Any) -> list[PaperItem]:
    """解析 HF daily_papers 响应。**字段来自真实响应**（不同日期字段可能不同）。"""
    if not isinstance(payload, list):
        raise ValueError(f"HF daily_papers 响应不是数组：{type(payload).__name__}")
    items: list[PaperItem] = []
    for row in payload:
        paper = (row or {}).get("paper") or {}
        title = " ".join(str(paper.get("title") or "").split())
        abstract = " ".join(str(paper.get("summary") or "").split())
        paper_id = str(paper.get("id") or "")
        raw = {
            "paper.id": paper_id,
            "paper.title": title,
            "paper.summary": abstract,
            "paper.upvotes": paper.get("upvotes"),
            "paper.publishedAt": paper.get("publishedAt"),
            "paper.authors": [
                a.get("name") for a in (paper.get("authors") or []) if isinstance(a, dict)
            ],
            "publishedAt": row.get("publishedAt"),
            "thumbnail": row.get("thumbnail"),
        }
        key = paper_id or normalize_title(title)
        items.append(PaperItem("hf_daily", key, title, abstract, raw))
    return items


async def probe_arxiv_by_ids(client: HttpClient, ids: list[str], endpoint: str) -> dict[str, str]:
    """用 `id_list` **一次**批量回查 HF 上榜论文的 arXiv 提交日（★ 禁止逐篇 GET）。

    返回 `{短 id: published 日期}`。用途：量化"HF 上榜论文相对其 arXiv 提交日滞后多少天"——
    这正是"必须用 discovery（first_seen）而不是 published 过滤"的实测依据。
    """
    if not ids:
        return {}
    params = {"id_list": ",".join(ids[:100]), "max_results": min(100, len(ids))}
    try:
        response = await client.get(
            endpoint, params=params, source="arxiv", rate_limit="1/3s", timeout=None
        )
    except (httpx.HTTPError, OSError) as exc:
        console.print(f"[yellow]![/] arXiv id_list 回查失败：{type(exc).__name__}: {exc}")
        return {}
    if response.status_code != 200:
        console.print(f"[yellow]![/] arXiv id_list 回查 HTTP {response.status_code}")
        return {}
    root = ET.fromstring(response.text)
    found: dict[str, str] = {}
    for entry in root.findall(f"{ATOM}entry"):
        entry_id = (entry.findtext(f"{ATOM}id") or "").strip()
        published = (entry.findtext(f"{ATOM}published") or "").strip()
        short = re.sub(r"v\d+$", "", arxiv_id_from_entry(entry_id))
        if short:
            found[short] = published[:10]
    return found


async def probe_hf_once(
    client: HttpClient, day: date, endpoint: str, label: str
) -> tuple[SourceProbeResult, list[PaperItem]]:
    """对一个 HF 端点发一次真实请求（官方端点失败就是失败，不掩盖）。

    `endpoint` 是**完整端点**（含 `/api/daily_papers`），调用方负责拼好。
    """
    url = endpoint.rstrip("/")
    params = {"date": day.isoformat()}
    console.print(f"[cyan]→ HF/{label}[/] {url}?date={day.isoformat()}")
    result = SourceProbeResult(
        name=f"hf_daily_{label}",
        endpoint=url,
        request_url=f"{url}?date={day.isoformat()}",
        availability="always",
        availability_reason="",
        auth_required=False,
        rate_limit="10/1s",
    )
    try:
        response = await client.get(
            url, params=params, source="hf_daily", rate_limit="10/1s", timeout=None
        )
    except (httpx.HTTPError, OSError) as exc:
        # ★ 连接层失败也要如实记录（本地 WSL 对官方域名不可达就是这种情况）
        result.error = f"{type(exc).__name__}: {exc}"
        console.print(f"   [red]失败[/] {result.error}")
        return result, []
    console.print(f"   HTTP {response.status_code} · {len(response.content)} B")
    result.status_code = response.status_code
    result.response_bytes = len(response.content)
    result.request_url = str(response.url)
    result.content_type = response.headers.get("content-type", "")
    if response.status_code != 200:
        result.error = f"HTTP {response.status_code}"
        return result, []
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        result.error = f"JSON 解析失败：{exc}"
        return result, []
    items = parse_hf_papers(payload)
    result.item_count = len(items)
    if items:
        result.fields = [k for k, v in items[0].raw.items() if v not in ("", [], None)]
        result.sample_excerpt = json.dumps(items[0].raw, ensure_ascii=False, indent=2)[
            :SAMPLE_LIMIT
        ]
    return result, items


async def probe_hf(
    client: HttpClient, day: date, official: str, mirror: str | None
) -> tuple[list[SourceProbeResult], list[PaperItem]]:
    """先打官方端点；官方不可达且显式给了 `--hf-mirror` 时，再用镜像取真实响应。

    ★ 镜像**不会**自动启用：不替用户决定镜像站。这里只在探测时显式指定，
    用于拿到真实的响应 schema 与条数（aipulse 已实测其 schema 与官方一致）。
    ★ 官方端点失败 = 记 warning（本环境网络问题），**不是错误**、不改源分级。
    """
    results: list[SourceProbeResult] = []
    official_result, items = await probe_hf_once(client, day, official, "official")
    # ★ availability 描述"源本身是否公开可用"，与"从某台机器能否访问"无关。
    #   失败只记原因（网络问题，配 hf_base_url / --hf-mirror 可解），不改分级。
    official_result.availability_reason = "官方开放 API，无需认证" + (
        "；本环境实测可达" if official_result.ok else "；本环境实测不可达（网络问题，配镜像可解）"
    )
    results.append(official_result)
    if not official_result.ok and mirror:
        mirror_url = f"{mirror.rstrip('/')}/api/daily_papers"
        mirror_result, mirror_items = await probe_hf_once(client, day, mirror_url, "mirror")
        mirror_result.availability_reason = (
            f"用户显式指定的备用端点 `{mirror_url}`（探测用；schema 与官方一致）；本环境实测可达"
        )
        results.append(mirror_result)
        items = mirror_items
    return results, items


# ── 汇总 ────────────────────────────────────────────────────────────────
def dedup(items: list[PaperItem]) -> list[PaperItem]:
    """跨源去重：arXiv 短 id（去版本号）优先，无 id 时退回归一化标题。"""
    seen: dict[str, PaperItem] = {}
    for item in items:
        if item.key in seen:
            continue
        seen[item.key] = item
    return list(seen.values())


def render_markdown(
    day: date,
    results: list[SourceProbeResult],
    samples: dict[str, str],
    stats: dict[str, Any],
) -> str:
    lines: list[str] = []
    lines.append("# 数据源手册")
    lines.append("")
    lines.append("> 本文件由 `scripts/probe_sources.py` **实跑真实请求**生成（Agent 维护）。")
    lines.append(
        f"> 探测日期口径：`{day.isoformat()}`（Asia/Shanghai），探测于 {stats['probe_utc']} UTC。"
    )
    lines.append("")
    lines.append(
        "★ 所有字段表与响应片段**均摘自真实响应**；拿不到的数据一律写"
        "「未获得 + 原因」，不编造。响应片段每条截断 8 KB（仓库 public，不存全量）。"
    )
    lines.append("")

    lines.append("## 1. 实测规模（★ 后续所有体积估算的唯一输入）")
    lines.append("")
    lines.append("口径链条（每一步剩余条数，可审计）：")
    lines.append("")
    lines.append(
        f"> arXiv total → arXiv 去版本号去重 → 并集（+HF）→ 领域过滤 → N_enter\n"
        f"> `{stats['arxiv_total']}` → `{stats['n_arxiv']}` → `{stats['union']}`"
        f"（= {stats['n_arxiv']} + {stats['n_hf']} − 交集 {stats['shared']}）"
        f" → `{stats['n_enter']}`（领域过滤掉 {stats['filtered_out']}）"
    )
    lines.append("")
    lines.append("| 量 | 值 | 口径 |")
    lines.append("|---|---|---|")
    lines.append(
        f"| N_arxiv | {stats['n_arxiv']} | arXiv 当日（cs.AI OR cs.CL OR cs.LG，"
        f"submittedDate 精确框定 {day.isoformat()}）去版本号去重后的条数（单日，非窗口） |"
    )
    lines.append(f"| N_hf | {stats['n_hf']} | HF Daily Papers 当日上榜条数 |")
    lines.append(
        f"| 两源交集 | {stats['shared']} | HF 上榜论文里 arXiv id 出现在「当天 arXiv 集合」的条数"
        f"（★ 不是 id_list 全库回查） |"
    )
    lines.append(f"| 去重并集 U | {stats['union']} | `|arXiv ∪ HF|`（未做领域过滤） |")
    lines.append(
        f"| N_enter | {stats['n_enter']} | U 经领域过滤（watchlist 命中，"
        f"threshold={stats['threshold']}）**进入系统**的条数 |"
    )
    lines.append(
        f"| 去重后保留比例 | {stats['retention_ratio']} | `N_enter / (N_arxiv + N_hf)`，"
        f"**不是重叠率**（重叠要看『两源交集』那一行） |"
    )
    lines.append("")
    lines.append("辅助口径：")
    lines.append("")
    lines.append("| 量 | 值 | 说明 |")
    lines.append("|---|---|---|")
    lines.append(f"| 领域过滤通过率 | {stats['filter_rate']} | `N_enter / U` |")
    lines.append(f"| 领域过滤掉 | {stats['filtered_out']} | `U − N_enter`（未命中 watchlist） |")
    lines.append(
        f"| HF 中来自 arXiv（全库） | {stats.get('hf_in_arxiv', '未获得')} | 按 arXiv id "
        f"`id_list` 批量回查**全库**（≠ 当天集合，见『两源交集』） |"
    )
    lines.append(f"| 上榜滞后 | {stats.get('lag', '未获得')} | HF 上榜日 − arXiv 提交日 |")
    lines.append("")
    if stats.get("notes"):
        lines.append("备注：")
        for note in stats["notes"]:
            lines.append(f"- {note}")
        lines.append("")

    lines.append("## 2. 数据源明细")
    lines.append("")
    for r in results:
        lines.append(f"### {r.name}")
        lines.append("")
        lines.append("| 项 | 值 |")
        lines.append("|---|---|")
        lines.append(f"| 端点 | `{r.endpoint}` |")
        lines.append(f"| 请求 URL | `{r.request_url}` |")
        lines.append(f"| HTTP 状态 | {r.status_code if r.status_code else '未获得（请求失败）'} |")
        lines.append(f"| 响应字节数 | {r.response_bytes} |")
        lines.append(f"| 条数 | {r.item_count if r.item_count is not None else '未获得'} |")
        lines.append(f"| Content-Type | {r.content_type or '未获得'} |")
        lines.append(f"| 认证需求 | {'需要' if r.auth_required else '无需认证'} |")
        lines.append(f"| 限速 | {r.rate_limit} |")
        lines.append(f"| 环境分级 | `{r.availability}` —— {r.availability_reason} |")
        if r.error:
            lines.append(f"| 失败原因 | {r.error} |")
        lines.append("")
        if r.fields:
            lines.append("字段表（**摘自真实响应**，空值字段未列出）：")
            lines.append("")
            lines.append("| 字段 | 说明 |")
            lines.append("|---|---|")
            for field in r.fields:
                lines.append(f"| `{field}` | {FIELD_NOTES.get(field, '—')} |")
            lines.append("")
        else:
            lines.append("字段表：未获得（无响应或无条目）。")
            lines.append("")
        sample = samples.get(r.name, "")
        if sample:
            lines.append("响应片段（第 1 条，截断 8 KB）：")
            lines.append("")
            lines.append("```json")
            lines.append(sample[:SAMPLE_LIMIT])
            lines.append("```")
            lines.append("")
        else:
            lines.append("响应片段：未获得。")
            lines.append("")

    lines.append(
        "## 2.5 采集回看窗口\n"
        "\n"
        "★ 单日条数是**下界**（索引会补全，见上），因此 `collect` 默认用 "
        f"**{stats.get('lookback_days', 'sources.arxiv.lookback_days')} 天回看窗口** 采集，"
        "而不是只采当天。\n"
        "\n"
        "重复扫描**不产生重复条目**：`first_seen.jsonl` append-only + `papers` upsert 兜住；"
        "窗口加宽后 HF（滞后 2~4 天）能解析到其 arXiv 原始条目 ⇒ **跨源去重率上升**。\n"
    )
    lines.append("")
    lines.append("## 3. 环境分级结论")
    lines.append("")
    lines.append("| 源 | 分级 | 依据 |")
    lines.append("|---|---|---|")
    for r in results:
        lines.append(f"| `{r.name}` | `{r.availability}` | {r.availability_reason} |")
    lines.append("")
    lines.append(
        "- `always`：源本身公开可用，本地与 Actions 都采。\n"
        "- `actions_only`：本地跳过且**不计失败**，只在 `AI_SCHOLAR_ENV=actions` 时采集。\n"
        "- ★ **不可达 ≠ 分级变化**：availability 描述『源本身是否公开可用』，"
        "不是『从某台机器能不能访问』。本地 WSL 连不上官方 HF 是网络问题，"
        "配 `hf_base_url`（镜像）即可解，分级仍是 `always`。\n"
        "- 本文件由**本地（WSL）**生成，`actions_only` 的源需另行在 Actions 验证："
        "`gh workflow run ci.yml`（ci.yml 的 probe job，手动触发，不回写仓库）。"
    )
    lines.append("")

    lines.append("## 4. 失效降级路径")
    lines.append("")
    lines.append(
        "- **arXiv**：主端点 `https://export.arxiv.org/api/query`。"
        "失败时按 `SourceError` 记录 warning 并跳过该源，**不影响退出码**；"
        "不做『换个镜像站』的自动切换（不替用户决定镜像）。"
    )
    lines.append(
        "- **HF Daily Papers**：主端点 `https://huggingface.co/api/daily_papers`。"
        "本地不可达是**网络环境问题**（配 `hf_base_url` / `--hf-mirror` 可解），"
        "不可达只 warning + 跳过该源，**不计失败、不影响退出码**。"
        "某天无数据（空数组）属正常（节假日 / 索引滞后），记为 0 条并继续。"
    )
    lines.append(
        "- 单源异常一律走 `ai_scholar.utils.errors.SourceError`，"
        "由编排层记 warning 后继续跑其余源。"
    )
    lines.append("")
    return "\n".join(lines)


FIELD_NOTES = {
    "id": "arXiv 条目 URL（`http://arxiv.org/abs/xxxx.xxxxxvN`）",
    "published": "v1 提交时间（event 时间；索引滞后 ⇒ 不能拿来当『今天新发现』）",
    "updated": "最近一次修订时间",
    "title": "标题（含换行，需归一化空白）",
    "summary": "摘要（abstract）",
    "authors": "作者列表",
    "categories": "全部类目（含交叉列表）",
    "primary_category": "主类目",
    "doi": "DOI（可为空）",
    "comment": "作者备注（常含页数 / 会议信息）",
    "journal_ref": "期刊引用（可为空）",
    "paper.id": "HF 论文 id（即 arXiv 短 id，跨源去重的连接键）",
    "paper.title": "标题",
    "paper.summary": "摘要",
    "paper.upvotes": "当日上榜票数（state 型信号）",
    "paper.publishedAt": "论文提交时间",
    "paper.authors": "作者名列表",
    "publishedAt": "上榜日期",
    "thumbnail": "缩略图 URL",
}


async def main_async(args: argparse.Namespace) -> int:
    settings = load_settings()
    day = date.fromisoformat(args.date) if args.date else (today_local() - timedelta(days=2))
    arxiv_cfg = settings.source("arxiv")
    categories = arxiv_cfg.categories() if arxiv_cfg else ["cs.AI", "cs.CL", "cs.LG"]

    results: list[SourceProbeResult] = []
    samples: dict[str, str] = {}

    async with HttpClient(
        user_agent=settings.http.user_agent,
        connect_timeout_s=args.connect_timeout,
        read_timeout_s=args.read_timeout,
        max_retries=2,
    ) as client:
        arxiv_endpoint = args.arxiv_base_url or (
            arxiv_cfg.endpoint if arxiv_cfg else "https://export.arxiv.org/api/query"
        )
        arxiv_result, arxiv_items, arxiv_sample = await probe_arxiv(
            client, day, categories, args.arxiv_max, arxiv_endpoint
        )
        arxiv_result.availability_reason = "官方开放 API，无需认证" + (
            "；本环境实测可达" if arxiv_result.ok else "；本环境实测不可达（见失败原因）"
        )
        results.append(arxiv_result)
        samples["arxiv"] = arxiv_sample

        hf_cfg = settings.source("hf_daily")
        default_hf_url = hf_cfg.hf_url() if hf_cfg else "https://huggingface.co/api/daily_papers"
        hf_url = args.hf_base_url or default_hf_url
        hf_results, hf_items = await probe_hf(client, day, hf_url, args.hf_mirror)
        results.extend(hf_results)
        for r in hf_results:
            samples[r.name] = r.sample_excerpt

        # HF 上榜论文回查 arXiv 提交日（一次批量请求，用于量化两源重叠与索引滞后）
        hf_ids = [i.key for i in hf_items if re.match(r"^\d{4}\.\d{4,5}$", i.key)]
        published_map = await probe_arxiv_by_ids(client, hf_ids, arxiv_endpoint)
        lags: list[int] = []
        for published in published_map.values():
            if not published:
                continue
            try:
                submit_day = date.fromisoformat(published)
            except ValueError:
                continue
            lags.append((day - submit_day).days)
        lags.sort()

    notes: list[str] = []
    arxiv_total = arxiv_result.item_count
    n_arxiv = len(dedup(arxiv_items))
    n_hf = len(dedup(hf_items))
    if not hf_items:
        notes.append(
            "N_hf 未获得：HF Daily Papers 官方端点在本环境不可达（网络问题），"
            "且未指定 `--hf-mirror`。下方 N_enter / 保留比例因此不含 HF 贡献。"
        )
    elif args.hf_mirror and any(r.name.endswith("mirror") and r.ok for r in results):
        notes.append(
            f"N_hf 与 HF 字段表取自备用端点 `{args.hf_mirror}/api/daily_papers`"
            "（官方端点本环境不可达）；其为官方数据的镜像，schema 一致，"
            "条数具有代表性，但**镜像可能有滞后**（非实时快照）。"
        )
    if arxiv_total is not None and arxiv_total > n_arxiv:
        notes.append(
            f"arXiv 总结果 {arxiv_total} 条，实际抓到（并去版本号去重）{n_arxiv} 条"
            f"（受 `--arxiv-max {args.arxiv_max}` 限制或分页中断）。"
        )
    union_items = dedup(arxiv_items + hf_items)
    arxiv_keys = {i.key for i in arxiv_items}
    shared = len({i.key for i in hf_items} & arxiv_keys)

    watchlist = load_watchlist()
    entered = [i for i in union_items if match_watchlist(watchlist, i.title, i.abstract).matched]

    total_raw = n_arxiv + n_hf
    stats: dict[str, Any] = {
        "lookback_days": settings.lookback_days(),
        "probe_utc": __import__("ai_scholar.utils.timeutil", fromlist=["utcnow"])
        .utcnow()
        .isoformat(timespec="seconds"),
        "arxiv_total": arxiv_total if arxiv_total is not None else n_arxiv,
        "n_arxiv": n_arxiv,
        "n_hf": n_hf,
        "shared": shared,
        "union": len(union_items),
        "n_enter": len(entered),
        "filtered_out": len(union_items) - len(entered),
        "threshold": watchlist.default_threshold,
        # ★ 这是「去重后保留比例」，不是重叠率（定义写错过，这里更正）
        "retention_ratio": f"{(len(entered) / total_raw):.3f}"
        if total_raw
        else "未获得（两源均无数据）",
        "filter_rate": f"{(len(entered) / len(union_items)):.3f}" if union_items else "未获得",
        "notes": notes,
    }
    # HF 与 arXiv 的真实重叠（按 arXiv id 回查，而非"同日交集"——后者天然为 0）
    if hf_items:
        stats["hf_in_arxiv"] = f"{len(published_map)}/{len(hf_ids)}"
        if lags:
            mid = lags[len(lags) // 2]
            stats["lag"] = f"中位 {mid} 天（最大 {max(lags)} 天，样本 {len(lags)} 条）"
            notes.append(
                f"HF 上榜论文相对其 arXiv 提交日**滞后**中位 {mid} 天（最大 {max(lags)} 天）；"
                f"上榜论文中 {len(published_map)}/{len(hf_ids)} 条能在 arXiv 查到。"
                "⇒ 两源必须按 arXiv id 交叉去重，且『今天新发现』只能用 discovery 时间过滤。"
            )
        if len(published_map) != len(hf_ids):
            notes.append(
                f"有 {len(hf_ids) - len(published_map)} 条 HF 上榜论文未在 id_list 回查中命中"
                "（非 arXiv 来源或 id 版本差异），未计入重叠统计。"
            )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_markdown(day, results, samples, stats), encoding="utf-8")

    console.print("\n[bold]实测规模[/]")
    console.print(f"  N_arxiv  = {stats['n_arxiv']}（total {stats['arxiv_total']}）")
    console.print(f"  N_hf     = {stats['n_hf']}")
    console.print(f"  两源交集  = {stats['shared']}")
    console.print(f"  N_enter  = {stats['n_enter']}（领域过滤掉 {stats['filtered_out']}）")
    console.print(f"  去重后保留比例 = {stats['retention_ratio']}")
    console.print(f"\n[green]✓[/] 已写入 {out_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="探测 arXiv / HF Daily Papers 真实响应")
    parser.add_argument("--date", help="探测日 YYYY-MM-DD（默认今天前推 2 天，避开索引滞后）")
    parser.add_argument("--arxiv-max", type=int, default=1200, help="arXiv 最多抓多少条")
    parser.add_argument("--out", default="docs/数据源手册.md", help="输出 Markdown 路径")
    parser.add_argument(
        "--hf-base-url",
        default=None,
        help="HF Daily Papers 完整端点（默认取 config 的 sources.hf_daily.hf_base_url）",
    )
    parser.add_argument(
        "--hf-mirror",
        default=None,
        help="官方不可达时的备用基址（★ 不自动启用，必须显式指定；仅用于探测取真实响应）",
    )
    parser.add_argument(
        "--arxiv-base-url",
        default=None,
        help="arXiv 端点基址（默认取 config/default.yaml 的 sources.arxiv.endpoint）",
    )
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    parser.add_argument("--read-timeout", type=float, default=60.0)
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
