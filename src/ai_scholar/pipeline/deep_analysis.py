"""深度分析（L3b-2，strong）：三句半 + 六段 + 背景透镜。

★ 触发（§5.5.2）：
- 通道 A（意图驱动，**不看 Tier**）：`--ids` 显式 > watchlist 命中 > 当日 Top N。
- 通道 B（价值驱动）：Tier 1 且未深度分析的补读队列。
- 注意力预算：每周最多 `deep_analysis.weekly_limit` 篇（默认 5），超限进队列不丢弃。

★ 正文：arXiv HTML **临时抓取**，抓完即弃，**永不入库**；抓不到退回仅摘要
（透镜块由 prompt 标【需正文】）。

★ 持久化：Markdown 全文 + `deep_content_hash` 幂等；失败标 `deep_status="failed"`，
不写幂等指纹（下次重试）。
"""

from __future__ import annotations

import os
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from sqlmodel import Session, SQLModel, col, create_engine, select

from ai_scholar.budget import check_budget, estimate_cost
from ai_scholar.collectors.fulltext import fetch_arxiv_html
from ai_scholar.config import (
    RunMode,
    Settings,
    load_classics,
    load_settings,
    load_watchlist,
    match_watchlist,
)
from ai_scholar.export.obsidian import dump_frontmatter, parse_frontmatter
from ai_scholar.llm import LLMClient, NullLLM, build_client
from ai_scholar.models import WatchlistConfig
from ai_scholar.pipeline.llm_support import (
    actual_cost,
    authors_text,
    estimate_input_tokens,
    references_text,
    render_prompt,
    spent_in_windows,
    window_starts,
    year_text,
)
from ai_scholar.store.db import default_db_path
from ai_scholar.store.models import (
    LINK_TYPE_REFERENCES,
    SIGNAL_REJECTED,
    SIGNAL_SAMPLE,
    LLMUsage,
    Paper,
    PaperLink,
)
from ai_scholar.utils.errors import LLMError
from ai_scholar.utils.http import HttpClient, client_from_settings
from ai_scholar.utils.timeutil import age_days_since, today_local


@dataclass
class DeepAnalysisReport:
    """一次 analyze 的汇总（CLI 据此打印，退出码不受降级影响）。"""

    run_id: str = ""
    mode: str = "strong"
    requested: int = 0
    analyzed: int = 0
    blocked: int = 0
    skipped: int = 0
    llm_calls: int = 0
    cost_cny: float = 0.0
    fulltext_hits: int = 0
    # ★ L7-4：产物落 public 真源 `data/analyses/{id}.md`（Option B）
    analyses_written: int = 0
    # ★ `--check`：缺失 / 过期的产物（退出码 1 的依据）
    analyses_missing: list[str] = field(default_factory=list)
    check: bool = False
    degraded: dict[str, str] = field(default_factory=dict)
    dry_run: bool = False


def _needs_analysis(paper: Paper, *, force: bool) -> bool:
    if force:
        return True
    if not paper.deep_analysis_md:
        return True
    return paper.deep_content_hash != paper.content_hash


def _watched(paper: Paper, watchlist: WatchlistConfig) -> bool:
    return match_watchlist(watchlist, paper.title, paper.abstract or "").matched


def _citation_leaders(session: Session, top_n: int) -> set[str]:
    """库内**引用榜**：被本库论文引用次数最多的 `Top N`（零成本、数据驱动）。

    ★ 这是 D 方案经典侧的第三个来源（§5.5「经典论文获取」②）：偏向近年高频工作，
      与种子清单互补 —— 清单是人工审校的公认经典，引用榜是数据发现的实际影响力。
      注意：引用榜里**没有**的被引论文（S2 出边指向库外 id）不参与，避免拉进库外条目。
    """
    if top_n <= 0:
        return set()
    rows = session.exec(select(PaperLink).where(PaperLink.link_type == LINK_TYPE_REFERENCES)).all()
    counts = Counter(row.dst_id for row in rows)
    # ★ 只认**库内**的被引论文：出边常指向库外 id（S2 给的是全图引用）
    known = {pid for pid in session.exec(select(Paper.id)).all() if pid}
    ranked = sorted(
        ((pid, n) for pid, n in counts.items() if pid in known),
        key=lambda t: (-t[1], t[0]),
    )
    return {pid for pid, _n in ranked[:top_n]}


def _heat_side(candidates: list[Paper], *, day: date, lookback_days: int) -> list[Paper]:
    """热度侧：`hf_score` 非空的近期新增，按热度降序（缺失**不当作 0**，直接不入侧）。"""
    cutoff = (day - timedelta(days=lookback_days)).isoformat()
    rows = [p for p in candidates if p.hf_score is not None and (p.first_seen_at or "") >= cutoff]
    rows.sort(key=lambda p: ((p.hf_score or 0), p.first_seen_at or ""), reverse=True)
    return rows


def _classic_side(
    candidates: list[Paper],
    *,
    day: date,
    maturity_days: int,
    min_citations: int,
    classic_ids: set[str],
    leaders: set[str],
) -> list[Paper]:
    """经典侧：成熟（≥180 天）**或** 被引达标（≥3）**或** 种子清单 / 库内引用榜。

    按被引降序；成熟但被引未知的老论文按出版日兜底（★ 不合成总分，只排序）。
    """

    def is_classic(paper: Paper) -> bool:
        if paper.arxiv_id and paper.arxiv_id in classic_ids:
            return True
        if paper.id in leaders:
            return True
        if (paper.citation_count or 0) >= min_citations:
            return True
        age = age_days_since(paper.published_at, day)
        return age is not None and age >= maturity_days

    rows = [p for p in candidates if is_classic(p)]
    rows.sort(key=lambda p: ((p.citation_count or 0), p.published_at or ""), reverse=True)
    return rows


def _alternate(heat: list[Paper], classic: list[Paper], limit: int) -> list[Paper]:
    """**交替取两侧最高分**：第 1/3/5… 篇取热度侧，第 2/4/6… 篇取经典侧。

    ★ 一侧取尽则退化为另一侧（"初期经典池为空 ⇒ 自动退化为热度侧"就是这个分支），
      两侧都空 ⇒ 返回空（交于通道 B 兜底）。
    """
    chosen: list[Paper] = []
    seen: set[str] = set()
    for i in range(limit):
        first, second = (heat, classic) if i % 2 == 0 else (classic, heat)
        pick = next((p for p in first if p.id not in seen), None) or next(
            (p for p in second if p.id not in seen), None
        )
        if pick is None:
            break
        seen.add(pick.id)
        chosen.append(pick)
    return chosen


def _select(
    session: Session,
    *,
    ids: list[str] | None,
    watchlist: WatchlistConfig,
    limit: int,
    a_cap: int,
    force: bool,
    today: date,
    settings: Settings,
) -> tuple[list[Paper], int]:
    """选篇：显式 `--ids` > 通道 A（D 方案交替）> 通道 B（Tier 1 补读）。

    ★ D 方案去重：**已读**（`deep_analysis_md` 非空且 hash 未变）与**已收藏**
      （`selected=1`，用户已点名要读）都不占用每周 5 篇的注意力预算。
    """
    papers = list(session.exec(select(Paper)).all())
    by_id = {p.id: p for p in papers}

    if ids:
        chosen = [by_id[i] for i in ids if i in by_id]
        eligible = list(chosen)
        chosen = [p for p in chosen if _needs_analysis(p, force=force)]
        return chosen[:limit], len(eligible) - len(chosen)

    pool = [
        p
        for p in papers
        if p.signal_state not in (SIGNAL_REJECTED, SIGNAL_SAMPLE)
        and (_watched(p, watchlist) or p.tier == 1)
    ]
    skipped = sum(1 for p in pool if not _needs_analysis(p, force=force))
    # ★ 已收藏（`selected=1`）不占自动配额；但 `--force` 是用户显式要求重做 ⇒ 优先
    candidates = [p for p in pool if _needs_analysis(p, force=force) and (force or not p.selected)]

    # ── 通道 A（意图驱动，立刻执行，不看 Tier）────────────────────────────
    #   ① watchlist 命中：按命中分降序，最多 a_cap 篇（用户点名的兴趣优先）
    watched = [(p, match_watchlist(watchlist, p.title, p.abstract or "").score) for p in candidates]
    watched = [item for item in watched if _watched(item[0], watchlist)]
    watched.sort(key=lambda t: (t[1], t[0].first_seen_at or ""), reverse=True)
    chosen = [p for p, _ in watched[:a_cap]]

    #   ② 当日 Top N（**D 方案**：热度 / 经典交替）：补"兴趣外的重要工作"
    #      —— 只按 watchlist 排会漏掉它们（方案 B 的缺陷）
    if len(chosen) < a_cap:
        rest = [p for p in candidates if p.id not in {c.id for c in chosen}]
        heat = _heat_side(
            rest,
            day=today,
            lookback_days=settings.deep_analysis.trending_lookback_days,
        )
        classic = _classic_side(
            rest,
            day=today,
            maturity_days=settings.signals.maturity_days,
            min_citations=settings.signals.min_citations,
            classic_ids=set(load_classics().ids()),
            leaders=_citation_leaders(session, settings.deep_analysis.citation_leaders),
        )
        chosen += _alternate(heat, classic, a_cap - len(chosen))

    # ── 通道 B：Tier 1 补读（填满 limit）─────────────────────────────────
    if len(chosen) < limit:
        chosen_ids = {p.id for p in chosen}
        backlog = [p for p in candidates if p.tier == 1 and p.id not in chosen_ids]
        backlog.sort(key=lambda p: ((p.citation_count or 0), p.first_seen_at or ""), reverse=True)
        chosen.extend(backlog[: limit - len(chosen)])
    return chosen, skipped


def validate_output(
    content: str,
    *,
    tokens_out: int | None = None,
    max_tokens: int | None = None,
) -> tuple[bool, str]:
    """校验深度分析输出（★ 断言打在**真实结构**上，不打在"模型是否听话"上）。

    背景（2026-10-26 Actions 实测）：原校验是「`paper.id` 必须出现在正文里」——
    但真实模型**经常不回显** id（本地 3 篇产物里 2 篇没有回显），
    结果 5 次调用只有 1 次通过，4 次花了钱被判废（¥0.2361 里 80% 是废调用）。

    ⇒ 改为结构校验：六段/TL;DR 骨架 + 最小长度。id 映射由「一次调用只喂一篇」保证，
      不依赖模型回显（回显只是 prompt 的要求，不是数据完整性的必要条件）。
    """
    if not content:
        if tokens_out is not None and max_tokens and tokens_out >= max_tokens - 1:
            return False, f"推理占满全部额度，答案为空（tokens_out={tokens_out}/{max_tokens}）"
        return False, "输出为空"
    if len(content) < MIN_ANALYSIS_CHARS:
        return False, f"输出过短（<{MIN_ANALYSIS_CHARS} 字符）"
    if "## 六段" not in content and "TL;DR" not in content:
        return False, "缺少 TL;DR / 六段骨架"
    # ★ E6：输出顶格 = 被截断（推理 token 吃掉了答案）。截断的"六段"是残稿，
    #   判成功会让 vault 里躺半篇 —— 必须判废（不写指纹，下次重试）。
    if tokens_out is not None and max_tokens and tokens_out >= max_tokens - 1:
        return False, f"输出顶格疑似截断（tokens_out={tokens_out}/{max_tokens}）"
    return True, ""


def _weekly_analyzed(session: Session, *, today: date) -> int:
    """本周**已产出**的深度分析篇数（配额口径 = 篇，不是调用次数）。

    ★ 为什么不用 `llm_usage` 计数（2026-10-26 Actions 实锤）：
      首次带 secret 的 run 里 5 次调用只有 1 次成功，但配额按**调用**记 ⇒
      4 次废调用直接吃掉整周额度，之后一周再也分析不了（`blocked=5`）。
      配额是"注意力预算"，语义是**篇**：成败都要省钱，但不能让失败把额度烧光。
      （成本侧另有 `budget.daily_limit_cny` 兜底，不会因为失败而无限花钱。）
    """
    start = window_starts(today)["weekly"]
    rows = session.exec(select(Paper).where(col(Paper.deep_analyzed_at).is_not(None))).all()
    return sum(1 for row in rows if (row.deep_analyzed_at or "") >= start.isoformat()[:10])


# ★ 输出最小长度：六段 + 三句半的合理下限；低于此视为被截断/敷衍
MIN_ANALYSIS_CHARS = 400


# ── L7-4：产物落 public 真源 `data/analyses/{id}.md`（Option B）─────────
ANALYSES_SCHEMA_VERSION = 1


def analyses_dir(settings: Settings) -> Path:
    """★ 深度分析产物的**真源目录**（public 仓库，`git` 管）。

    db 是可丢弃缓存：Actions 的 db 走 actions-cache，本地 db 是另一份 ——
    只有把产物写成 public 文件，"本地 `git pull` 后 `export` 物化进 vault"才成立。
    """
    return settings.data_dir() / "analyses"


def analysis_path(settings: Settings, paper_id: str) -> Path:
    safe = paper_id.replace(":", "-").replace("/", "-")
    return analyses_dir(settings) / f"{safe}.md"


def render_analysis_md(paper: Paper) -> str:
    """分析产物的完整文本：frontmatter（可审计）+ 正文。

    ★ 幂等：字段只取**内容级**信息（分析日期取 `deep_analyzed_at`，不是"今天"），
      同内容 ⇒ 同字节 ⇒ `--check` 可断言。**绝不读 vault**（public 产物不含私有内容）。
    """
    fields: dict[str, object] = {
        "id": paper.id,
        "title": paper.title,
        "arxiv_id": paper.arxiv_id,
        "analyzed_at": paper.deep_analyzed_at,
        "content_hash": paper.deep_content_hash,
        "schema_version": ANALYSES_SCHEMA_VERSION,
    }
    return f"{dump_frontmatter(fields)}\n{(paper.deep_analysis_md or '').strip()}\n"


def write_analysis(settings: Settings, paper: Paper) -> bool:
    """原子写 `data/analyses/{id}.md`；返回是否**真的写了**（内容相同 ⇒ 不写）。"""
    path = analysis_path(settings, paper.id)
    content = render_analysis_md(paper)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    return True


def sync_analyses(session: Session, settings: Settings) -> int:
    """把 db 里**已有**的分析补写成 public 真源（自愈）。

    ★ L7 之前跑过的分析只在 db 里 —— 真源会缺一大片，`analyze --check` 一上来就红。
      这里按"db 有 ⇒ 真源必须有"补写，产物缺失就不再是静默失败。
    """
    written = 0
    for paper in session.exec(select(Paper)).all():
        if not paper.deep_analysis_md:
            continue
        # ★ 与"分析完成即置位"保持一致：L7 之前分析过的论文当时没置位，
        #   真源补写时顺手补上，否则它们永远导不进 vault（DoD #3 的库内一致性）。
        if not paper.selected:
            paper.selected = True
        if write_analysis(settings, paper):
            written += 1
    return written


def analysis_is_current(settings: Settings, paper: Paper) -> bool:
    """`--check` 用：产物是否存在且未过期（frontmatter 的 `content_hash` 对得上）。

    ★ 用 `parse_frontmatter` 解析，**不做字符串包含匹配** —— 引号 / 转义会让
      `f"content_hash: {hash}" in text` 这类断言恒假（本项目已三次踩中，红线 18）。
    """
    path = analysis_path(settings, paper.id)
    if not path.exists() or not paper.deep_analysis_md:
        return False
    fields, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
    return fields.get("content_hash") == paper.content_hash


async def run_deep_analysis(
    *,
    ids: list[str] | None = None,
    limit: int | None = None,
    force: bool = False,
    dry_run: bool = False,
    check: bool = False,
    today: date | None = None,
    settings: Settings | None = None,
    db_path: Path | None = None,
    client: LLMClient | NullLLM | None = None,
    http_client: HttpClient | None = None,
    watchlist: WatchlistConfig | None = None,
) -> DeepAnalysisReport:
    """跑一次深度分析。★ off 档直接返回；失败只 degraded，不影响退出码。

    ★ `check=True`：**零 LLM、零网络** —— 只核对 `data/analyses/` 里的产物是否齐备
      （缺失 / `content_hash` 过期进 `report.analyses_missing`），供幂等断言。
    """
    settings = settings or load_settings()
    report = DeepAnalysisReport(dry_run=dry_run, check=check)
    report.run_id = uuid.uuid4().hex[:12]
    day = today or today_local()

    if settings.effective_mode() == RunMode.OFF and not check:
        # ★ off 档零调用（check 例外：它只读 db + 文件，与 LLM 档位无关）
        report.mode = "off"
        return report

    db_file = Path(db_path) if db_path else default_db_path(settings)
    engine = create_engine(f"sqlite:///{db_file}", echo=False)
    SQLModel.metadata.create_all(engine)

    effective_limit = limit if limit is not None else settings.deep_analysis.default_limit
    a_cap = min(settings.collect.deep_dive_top_n, effective_limit)
    weekly_limit = settings.deep_analysis.weekly_limit
    lens = settings.deep_analysis.lens
    provider = settings.llm.provider
    model = settings.llm.strong_model
    max_tokens = settings.llm.max_tokens_strong

    wl = watchlist or load_watchlist()
    owns_client = client is None
    llm = client or build_client(settings, tier="strong")
    owns_http = http_client is None
    http = http_client or client_from_settings(settings)
    calls = 0
    week_calls = 0
    # ★ 预算保护：连续 N 次判废 ⇒ 中止本轮（付费仍发生，不许把配额/预算一路烧完）
    consecutive_failures = 0
    max_consecutive_failures = 2

    try:
        with Session(engine) as session:
            if check:
                # ★ 零 LLM / 零网络：核对"分析过的论文"是否都有 public 产物
                #   （候选里还没分析的不算缺失 —— 那是"待做"，不是"产物丢了"）
                rows = [p for p in session.exec(select(Paper)).all() if p.deep_analysis_md]
                report.requested = len(rows)
                report.analyses_missing = [
                    p.id for p in rows if not analysis_is_current(settings, p)
                ]
                return report

            week_calls = _weekly_analyzed(session, today=day)
            # ★ 先自愈：db 里已有的分析必须落到 public 真源（否则 --check 永远红）
            report.analyses_written += sync_analyses(session, settings)
            candidates, skipped = _select(
                session,
                ids=ids,
                watchlist=wl,
                limit=effective_limit,
                a_cap=a_cap,
                force=force,
                today=day,
                settings=settings,
            )
            report.requested = len(candidates)
            report.skipped = skipped

            for i, paper in enumerate(candidates):
                if week_calls >= weekly_limit:
                    report.blocked = len(candidates) - i
                    report.degraded["quota"] = f"周配额已满（{weekly_limit} 篇）"
                    break

                fulltext = None
                if not dry_run and paper.arxiv_id:
                    fulltext = await fetch_arxiv_html(
                        http,
                        paper.arxiv_id,
                        max_chars=settings.deep_analysis.max_fulltext_chars,
                        min_chars=settings.deep_analysis.min_fulltext_chars,
                    )
                if fulltext:
                    report.fulltext_hits += 1

                days_since = age_days_since(paper.published_at, day)
                prompt = render_prompt(
                    "deep_analysis.j2",
                    paper_id=paper.id,
                    title=paper.title,
                    authors_and_affiliations=authors_text(paper),
                    year=year_text(paper),
                    venue=paper.venue or "—",
                    abstract=paper.abstract or "",
                    references=references_text(session, paper.id),
                    citation_count=paper.citation_count or 0,
                    days_since_published=days_since if days_since is not None else "—",
                    citing_titles=None,
                    has_fulltext=fulltext is not None,
                    fulltext=fulltext or "",
                    lens_profile=lens.profile,
                    lens_column_header=lens.column_header,
                    lens_q1_hint=lens.q1_hint,
                    lens_q2_hint=lens.q2_hint,
                    lens_q3_hint=lens.q3_hint,
                    lens_q4_hint=lens.q4_hint,
                    lens_q5_hint=lens.q5_hint,
                )
                estimate = estimate_cost(
                    settings.budget,
                    provider,
                    model,
                    estimate_input_tokens(prompt),
                    max_tokens,
                )
                state = spent_in_windows(session, today=day, calls_this_run=calls)
                decision = check_budget(settings.budget, state, estimate)
                if not decision.allowed:
                    report.blocked = len(candidates) - i
                    report.degraded["budget"] = decision.reason
                    break

                calls += 1
                week_calls += 1
                if dry_run:
                    report.analyzed += 1
                    report.llm_calls += 1
                    report.cost_cny += estimate.cost_cny
                    continue

                try:
                    response = await llm.complete(
                        [{"role": "user", "content": prompt}], json_mode=False
                    )
                except LLMError as exc:
                    paper.deep_status = "failed"
                    report.degraded["llm"] = f"{type(exc).__name__}: {exc}"
                    continue

                # ★ 真实调用即记账（即使输出校验失败，成本已发生）
                analyze_cost = actual_cost(settings, provider, model, response)
                session.add(
                    LLMUsage(
                        run_id=report.run_id,
                        model=model,
                        tokens_in=response.tokens_in,
                        tokens_out=response.tokens_out,
                        cost_cny=analyze_cost,
                        cache_hit=response.cache_hit,
                        purpose="deep_analysis",
                    )
                )
                report.llm_calls += 1
                report.cost_cny += analyze_cost

                content = response.content.strip()
                ok, reason = validate_output(
                    content, tokens_out=response.tokens_out, max_tokens=max_tokens
                )
                if not ok:
                    paper.deep_status = "failed"
                    # ★ E5：错误必须自证（带 paper_id / 长度 / tokens），且**不互相覆盖**
                    report.degraded[f"llm:{paper.id}"] = (
                        f"{reason}（{len(content)} 字符 / "
                        f"tokens_out={response.tokens_out}，下次重试）"
                    )
                    consecutive_failures += 1
                    if consecutive_failures >= max_consecutive_failures:
                        report.blocked = len(candidates) - i - 1
                        report.degraded["llm_abort"] = (
                            f"连续 {consecutive_failures} 次输出判废，中止本轮"
                            "（避免继续付费；已花成本见 cost）"
                        )
                        break
                    continue

                consecutive_failures = 0

                paper.deep_analysis_md = content
                paper.deep_analyzed_at = day.isoformat()
                paper.deep_content_hash = paper.content_hash
                paper.deep_status = "done"
                # ★ L7-3 置位途径 ①：深度分析完成 ⇒ selected=1
                #   （`export` 默认只导 selected —— 不置位，vault 里永远不会有这篇笔记）
                paper.selected = True
                report.analyzed += 1
                # ★ L7-4：产物落 public 真源（db 是可丢弃缓存，真源才能跨机恢复）
                if write_analysis(settings, paper):
                    report.analyses_written += 1

            if not dry_run:
                session.commit()
    finally:
        if owns_client:
            await llm.aclose()
        if owns_http:
            await http.aclose()

    return report
