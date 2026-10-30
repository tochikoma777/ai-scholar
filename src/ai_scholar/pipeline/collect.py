"""采集编排：源 → 三级去重 → upsert → `first_seen.jsonl` → `runs`。

★ 三条本阶段最容易踩的红线，都在这个文件里被守住：

1. **`content_hash` 不含引用数 / 热度**（`store/hash.py`）——否则每天重复入库。
2. **watchlist 不影响入库** —— 本模块**根本不 import watchlist**。
   领域过滤是 cs.AI/CL/LG，**已在 arXiv 查询的 `cat:` 里施加过**，
   不要再滤第二遍（设计方案 §4.2.1）。
3. **`first_seen` 绝不覆盖** —— JSONL append-only；db 里的值以 JSONL 为准。

★ 单源失败隔离：任一源失败只记 `degraded`，**不影响退出码**（退出码由 CLI 决定）。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel, col, select

from ai_scholar.collectors.arxiv import fetch_arxiv_day, fetch_arxiv_ids
from ai_scholar.collectors.base import SOURCE_HF, RawPaper
from ai_scholar.collectors.hf import fetch_hf_day, hf_collect_days
from ai_scholar.config import Settings, load_settings
from ai_scholar.store.db import default_db_path, engine_for
from ai_scholar.store.first_seen import FirstSeenStore, default_first_seen_path
from ai_scholar.store.hash import (
    canonical_arxiv_id,
    content_hash,
    normalize_doi,
    paper_id_for_arxiv,
    paper_id_for_doi,
)
from ai_scholar.store.models import LLMUsage, Paper, PaperAlias, Run
from ai_scholar.utils.http import HttpClient
from ai_scholar.utils.timeutil import utcnow

ALIAS_DOI = "doi"
ALIAS_ARXIV_VERSION = "arxiv_version"


@dataclass
class SourceOutcome:
    """单个源的采集结果。"""

    name: str
    fetched: int = 0
    new: int = 0
    updated: int = 0
    deduped: int = 0
    failed: bool = False
    degraded: str | None = None


@dataclass
class CollectReport:
    """一次采集的汇总（CLI 据此打印与决定退出码）。"""

    outcomes: list[SourceOutcome] = field(default_factory=list)
    first_seen_new: int = 0
    dry_run: bool = False
    run_id: str = ""
    degraded: dict[str, str] = field(default_factory=dict)
    db_path: str = ""

    @property
    def fetched(self) -> int:
        return sum(o.fetched for o in self.outcomes)

    @property
    def new(self) -> int:
        return sum(o.new for o in self.outcomes)

    @property
    def updated(self) -> int:
        return sum(o.updated for o in self.outcomes)

    @property
    def deduped(self) -> int:
        return sum(o.deduped for o in self.outcomes)

    @property
    def ok(self) -> bool:
        """是否有源失败（★ 失败只降级，不改变退出码）。"""
        return not any(o.failed for o in self.outcomes)


# ── 三级去重 + upsert ───────────────────────────────────────────────────


def canonical_id_for(raw: RawPaper) -> str:
    """一级（身份）：归一化主键。arXiv id 剥离版本号；DOI 前缀统一小写。"""
    if raw.arxiv_id:
        return paper_id_for_arxiv(raw.arxiv_id)
    if raw.doi:
        return paper_id_for_doi(raw.doi)
    # 兜底：既无 arXiv id 也无 DOI（理论上 HF 条目都带 arXiv id）
    return f"titlehash:{content_hash(raw.title, raw.abstract, raw.authors, raw.venue)[:16]}"


def aliases_for(raw: RawPaper) -> list[tuple[str, str]]:
    """三级（跨版本）候选别名：`(alias, kind)`。别名**不产生新条目**。"""
    out: list[tuple[str, str]] = []
    if raw.doi:
        out.append((paper_id_for_doi(raw.doi), ALIAS_DOI))
    if raw.arxiv_id and canonical_arxiv_id(raw.arxiv_id) != raw.arxiv_id:
        # 带版本号的原始 id（如 2610.22350v2）→ 归一到剥离版本号的主条目
        out.append((raw.arxiv_id, ALIAS_ARXIV_VERSION))
    return out


def _resolve_via_alias(session: Session, raw: RawPaper) -> str | None:
    """三级去重的读路径：先按别名找，命中则归一到已有 canonical。"""
    for alias, _kind in aliases_for(raw):
        row = session.get(PaperAlias, alias)
        if row is not None:
            return row.canonical_id
    return None


def _apply_observed(existing: Paper, raw: RawPaper) -> None:
    """只更新**观测值**（热度 / 引用数）——它们不进 hash，但照存。"""
    if raw.hf_score is not None:
        existing.hf_score = raw.hf_score
    if raw.citation_count is not None:
        existing.citation_count = raw.citation_count
    if raw.refs_count is not None:
        existing.refs_count = raw.refs_count


def upsert_paper(
    session: Session,
    raw: RawPaper,
    first_seen: FirstSeenStore,
    today_str: str,
    *,
    dry_run: bool = False,
) -> str:
    """upsert 一条，返回 `new` / `updated` / `deduped`。

    ★ 默认绝不重做：已存在的条目只更新，**不重新生成**（设计方案 §3.2.2）。
    """
    pid = canonical_id_for(raw)
    resolved = _resolve_via_alias(session, raw)
    if resolved and resolved != pid:
        # 三级：通过别名命中已有条目（预印本 ↔ 正式版归一）
        pid = resolved

    existing = session.get(Paper, pid)

    if existing is None:
        if dry_run:
            return "new"
        # ★ first_seen：真源是 JSONL（append-only，已有则不覆盖）
        first_seen.append(pid, today_str, raw.source)
        seen = first_seen.get(pid)
        first_seen_at = seen.first_seen_at if seen else today_str
        paper = Paper(
            id=pid,
            source=raw.source,
            primary_source=raw.source,
            # ★ db 里的 first_seen_at 只是缓存，以 JSONL 为准
            first_seen_at=first_seen_at,
            published_at=raw.published_at,
            title=raw.title,
            abstract=raw.abstract,
            authors_json=json.dumps(raw.authors, ensure_ascii=False),
            affiliations_json=json.dumps(raw.affiliations, ensure_ascii=False),
            venue=raw.venue,
            url=raw.url,
            arxiv_id=canonical_arxiv_id(raw.arxiv_id) or None,
            doi=normalize_doi(raw.doi) or None,
            primary_category=raw.primary_category,
            categories_json=json.dumps(raw.categories, ensure_ascii=False),
            hf_score=raw.hf_score,
            citation_count=raw.citation_count,
            refs_count=raw.refs_count,
            content_hash=content_hash(raw.title, raw.abstract, raw.authors, raw.venue),
        )
        session.add(paper)
        for alias, kind in aliases_for(raw):
            if session.get(PaperAlias, alias) is None:
                session.add(PaperAlias(alias=alias, canonical_id=pid, alias_kind=kind))
        return "new"

    # ── 已存在 ──────────────────────────────────────────────────────────
    # 二级（跨源）：★ HF 只补热度，**不覆盖元数据、不产生新条目**
    if raw.source == SOURCE_HF and existing.primary_source != SOURCE_HF:
        if not dry_run:
            _apply_observed(existing, raw)
            existing.updated_at = utcnow().isoformat(timespec="seconds")
        return "deduped"

    # 一级（身份）：内容变了才更新；否则只刷观测值
    new_hash = content_hash(raw.title, raw.abstract, raw.authors, raw.venue)
    if existing.content_hash != new_hash:
        if not dry_run:
            existing.title = raw.title
            existing.abstract = raw.abstract
            existing.authors_json = json.dumps(raw.authors, ensure_ascii=False)
            existing.venue = raw.venue
            existing.content_hash = new_hash
            existing.published_at = raw.published_at or existing.published_at
            existing.primary_category = raw.primary_category or existing.primary_category
            existing.categories_json = json.dumps(raw.categories, ensure_ascii=False)
            _apply_observed(existing, raw)
            existing.updated_at = utcnow().isoformat(timespec="seconds")
        return "updated"

    if not dry_run:
        _apply_observed(existing, raw)
        existing.updated_at = utcnow().isoformat(timespec="seconds")
    return "deduped"


def _record_aliases(session: Session, raw: RawPaper, canonical_id: str, *, dry_run: bool) -> None:
    """把别名补进 `paper_aliases`（已存在的不覆盖）。"""
    if dry_run:
        return
    for alias, kind in aliases_for(raw):
        if session.get(PaperAlias, alias) is None:
            session.add(PaperAlias(alias=alias, canonical_id=canonical_id, alias_kind=kind))


# ── 主流程 ──────────────────────────────────────────────────────────────


def _days_back(days: int, today: date) -> list[date]:
    return [today - timedelta(days=i) for i in range(days)]


def _failure_text(exc: BaseException, *, context: str) -> str:
    """★ E5：失败信息必须自证 —— 异常类型 + 消息 + 关键参数。

    Why（2026-10-26 实锤）：`httpx.ReadTimeout` 的 `str(exc)` 是**空串**，
    报告里只能显示「降级：ReadTimeout: 」，看不出哪天 / 哪个端点 / 多少秒超时，
    下一个人只能从零猜。⇒ 消息为空要显式标注，并补上请求的关键参数。
    """
    detail = " ".join(str(exc).split()) or "（异常未携带消息）"
    return f"{type(exc).__name__}: {detail}（{context}）"[:200]


async def _collect_source(
    name: str,
    client: HttpClient,
    settings: Settings,
    days: list[date],
    max_results: int,
) -> tuple[list[RawPaper], list[str], list[str]]:
    """抓一个源（跨多天）。**逐日隔离**：某天失败只记 degraded，其余天照常入库。

    返回 `(条目, 未命中的 id, 失败说明)`：
    - 未命中 id 只有 `classics` 源会有（arXiv 对不存在的 id 静默跳过）；
    - 失败说明形如 `2026-10-24: ReadTimeout: ...`，由调用方写进 `runs.degraded_json`。

    ★ 为什么改成逐日（2026-10-26 实测）：arXiv 单页读超时把**整天**打成异常，
      7 天窗口里 1 天超时 = 整个源 0 条（数据入口静默归零，比报错更危险）。
    """
    items: list[RawPaper] = []
    failures: list[str] = []
    if name == "classics":
        # ★ 经典种子清单直采（v1.11）：人工审校的 arXiv id，用 `id_list` 批量拉
        from ai_scholar.config import load_classics
        from ai_scholar.store.hash import canonical_arxiv_id

        cfg = settings.source("arxiv")
        endpoint = cfg.endpoint if cfg else "https://export.arxiv.org/api/query"
        rate_limit = cfg.rate_limit if cfg else "1/3s"
        ids = load_classics().ids()
        if not ids:
            raise RuntimeError("config/classics.yaml 为空 —— 清单缺失时不发请求")
        items, missing = await fetch_arxiv_ids(client, ids, endpoint, rate_limit=rate_limit)
        return items, [canonical_arxiv_id(i) for i in missing], failures
    if name == "arxiv":
        cfg = settings.source("arxiv")
        endpoint = cfg.endpoint if cfg else "https://export.arxiv.org/api/query"
        rate_limit = cfg.rate_limit if cfg else "1/3s"
        categories = cfg.categories() if cfg else ["cs.AI", "cs.CL", "cs.LG"]
        read_timeout = float(settings.collect.arxiv_read_timeout_s) or None
        for day in days:
            try:
                items.extend(
                    await fetch_arxiv_day(
                        client,
                        day,
                        categories,
                        endpoint,
                        max_results,
                        read_timeout_s=read_timeout,
                        rate_limit=rate_limit,
                    )
                )
            except Exception as exc:  # noqa: BLE001 —— 单日失败必须隔离
                context = f"{day.isoformat()} · {endpoint} · read_timeout={read_timeout}s"
                failures.append(_failure_text(exc, context=context))
    elif name == "hf_daily":
        cfg = settings.source("hf_daily")
        base_url = cfg.hf_url() if cfg else "https://huggingface.co/api/daily_papers"
        rate_limit = cfg.rate_limit if cfg else "10/1s"
        # ★ L6-1：HF 榜单滞后发布，请求「今天」必 400 ⇒ 窗口从昨天开始（不含今天）
        hf_days = hf_collect_days(days[0], len(days))
        for day in hf_days:
            try:
                items.extend(await fetch_hf_day(client, day, base_url, rate_limit=rate_limit))
            except Exception as exc:  # noqa: BLE001 —— 同样逐日隔离
                failures.append(_failure_text(exc, context=f"{day.isoformat()} · {base_url}"))
    return items, [], failures


def _source_gate(name: str, settings: Settings) -> str | None:
    """源级开关：`sources.*.enabled` 与 `availability` 的**唯一消费点**。

    ★ 两个键此前全仓无人读（E1 死配置，2026-10-29 审查）。`enabled=false` 或
      `availability=actions_only` 且当前不在 Actions ⇒ 跳过该源，并把原因写进
      `degraded`（**不是静默跳过** —— 静默跳过和"源挂了"在报告里长得一样）。
    """
    cfg_name = "arxiv" if name == "classics" else name
    cfg = settings.source(cfg_name)
    if cfg is None:
        return None
    if not cfg.enabled:
        return f"已跳过：源已在配置中禁用（sources.{cfg_name}.enabled=false）"
    if not cfg.is_available():
        reason = cfg.availability_reason or "未说明原因"
        return f"已跳过：源在当前环境不可用（availability={cfg.availability}；{reason}）"
    return None


async def run_collect(
    *,
    days: int = 1,
    source: str | None = None,
    dry_run: bool = False,
    settings: Settings | None = None,
    db_path: Path | None = None,
    first_seen_path: Path | None = None,
    today: date | None = None,
    client: HttpClient | None = None,
    classics: bool = False,
) -> CollectReport:
    """跑一次采集。

    ★ 本阶段**没有任何 LLM 调用** —— `llm_usage` 必然 0 行。
    ★ `classics=True`：只采 `config/classics.yaml` 的经典种子清单（arXiv `id_list` 直采）。
    """
    settings = settings or load_settings()
    report = CollectReport(dry_run=dry_run)
    report.run_id = uuid.uuid4().hex[:12]

    db_file = Path(db_path) if db_path else default_db_path(settings)
    report.db_path = str(db_file)
    if dry_run and not db_file.exists():
        # ★ dry-run 不该凭空造出 db 文件：统计在内存库里做
        engine = create_engine("sqlite:///:memory:", echo=False)
    else:
        engine = engine_for(db_file, settings)
    SQLModel.metadata.create_all(engine)  # 幂等：已存在则不动

    seen_path = Path(first_seen_path) if first_seen_path else default_first_seen_path(settings)
    first_seen = FirstSeenStore(seen_path)
    baseline_seen = len(first_seen)

    from ai_scholar.utils.timeutil import today_local

    day0 = today or today_local()
    day_list = _days_back(days, day0)
    today_str = day0.isoformat()

    names = ["classics"] if classics else ([source] if source else ["arxiv", "hf_daily"])
    max_results = settings.collect.arxiv_max_results

    owns_client = client is None
    http = client or HttpClient(user_agent=settings.http.user_agent)

    try:
        with Session(engine) as session:
            for name in names:
                outcome = SourceOutcome(name=name)
                gate = _source_gate(name, settings)
                if gate is not None:
                    outcome.failed = True
                    outcome.degraded = gate
                    report.degraded[name] = gate
                    report.outcomes.append(outcome)
                    continue
                try:
                    items, missing, failures = await _collect_source(
                        name, http, settings, day_list, max_results
                    )
                except Exception as exc:  # noqa: BLE001 —— 单源失败必须隔离
                    outcome.failed = True
                    outcome.degraded = _failure_text(
                        exc, context=f"source={name} · days={day_list[0]}~{day_list[-1]}"
                    )
                    report.degraded[name] = outcome.degraded
                    report.outcomes.append(outcome)
                    continue
                if failures and not items:
                    # 全部天都失败 ⇒ 仍是"源失败"（沿用原 key，CLI/报告据此标黄）
                    outcome.failed = True
                    outcome.degraded = failures[0]
                    report.degraded[name] = outcome.degraded
                elif failures:
                    # ★ 部分天失败：数据照常入库，但失败的天必须显式暴露
                    report.degraded[f"{name}_partial"] = "；".join(failures[:5])
                if missing:
                    report.degraded[f"{name}_missing"] = (
                        f"{len(missing)} 个 id 未命中：" + ", ".join(missing[:20])
                    )

                outcome.fetched = len(items)
                for raw in items:
                    result = upsert_paper(session, raw, first_seen, today_str, dry_run=dry_run)
                    setattr(outcome, result, getattr(outcome, result) + 1)
                # 别名补登记（新条目已在 upsert 里写过，这里兜住"先入库后才知道别名"的情况）
                for raw in items:
                    _record_aliases(session, raw, canonical_id_for(raw), dry_run=dry_run)
                report.outcomes.append(outcome)

            if not dry_run:
                # ★ 零成本预筛（L1-B）：只决定"是否跟踪"（pending/sample/rejected），
                #   **绝不决定入库** —— 论文已经全部写入 papers 表。
                from ai_scholar.pipeline.prescreen import apply_prescreen

                ungraded = list(
                    session.exec(select(Paper).where(col(Paper.signal_state).is_(None))).all()
                )
                for paper in ungraded:
                    apply_prescreen(
                        paper,
                        prescreen_cfg=settings.prescreen,
                        signals_cfg=settings.signals,
                        today=day0,
                    )

            if not dry_run:
                session.commit()

                # ★ runs：每次采集一行；降级状态显式暴露
                session.add(
                    Run(
                        run_id=report.run_id,
                        started_at=utcnow().isoformat(timespec="seconds"),
                        finished_at=utcnow().isoformat(timespec="seconds"),
                        mode=settings.effective_mode().value,
                        source=source,
                        fetched=report.fetched,
                        new=report.new,
                        updated=report.updated,
                        deduped=report.deduped,
                        degraded_json=json.dumps(report.degraded, ensure_ascii=False)
                        if report.degraded
                        else None,
                    )
                )
                session.commit()
    finally:
        if owns_client:
            await http.aclose()

    report.first_seen_new = len(first_seen) - baseline_seen
    return report


def llm_usage_rows(session: Session) -> list[LLMUsage]:
    """`llm_usage` 的行数（★ off 档必须为 0）。"""
    return list(session.exec(select(LLMUsage)).all())
