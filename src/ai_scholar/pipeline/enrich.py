"""补齐引用：S2 batch → `paper_links`（出边）→ 入边由反向查询得到。

★ 方向语义（§2.2，本阶段最容易建模错的地方）：

```
paper_links(src_id=A, dst_id=B, link_type='references')  ==  "A 引用了 B"
```

- **出边**：A 的 `references` 里有 B ⇒ 写 `(A, B)`
- **入边不单独写入**：查 `dst_id = A` 即得"谁引用了 A"（`idx_links_dst` 已建）
- ★ **只存库内目标的边**：被引目标不在 `papers` 表 ⇒ **不写边**，只累加 `refs_count`。
  理由：① 外部目标没有元数据，渲染不出来，写了就是"幽灵条目"
  ② 按需回采被引论文会引入 ≈1.5 万条/天（564 万行/年），库会失控
- **可解释性**：`evidence = "s2_references"`（引用是事实，无需 LLM 解释）

★ 冷启动期（§5.1b）：新论文引用的绝大多数是**库外老论文** ⇒ 边**必然稀疏**，
这是预期不是 bug。**不得**因"边很少"判定本阶段失败。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import desc
from sqlmodel import Session, SQLModel, col, create_engine, select

from ai_scholar.collectors.s2 import S2Record, S2Reference, fetch_s2_papers
from ai_scholar.config import Settings, load_settings
from ai_scholar.store.db import default_db_path
from ai_scholar.store.hash import paper_id_for_arxiv, paper_id_for_doi
from ai_scholar.store.models import (
    ENRICH_DONE,
    ENRICH_PENDING,
    ENRICH_STALE,
    EVIDENCE_S2_REFERENCES,
    LINK_TYPE_REFERENCES,
    Paper,
    PaperLink,
)
from ai_scholar.utils.errors import SourceError
from ai_scholar.utils.http import HttpClient
from ai_scholar.utils.timeutil import utcnow

S2_SOURCE_NAME = "semantic_scholar"


@dataclass
class EnrichReport:
    """一次 enrich 的汇总（CLI 据此打印，退出码不受降级影响）。"""

    run_id: str = ""
    requested: int = 0  # 本次处理的论文数
    fetched: int = 0  # S2 返回**非 null**的记录数（解析率分子）
    refs_seen: int = 0  # S2 返回的 references 总条数
    in_library_hits: int = 0  # 其中命中库内论文的条数
    out_links: int = 0  # 实际写入的出边数（去重后新增的行）
    cited_papers: int = 0  # distinct dst ⇒ 可反向查到的"被引用"条目数
    done: int = 0
    pending: int = 0
    stale: int = 0
    degraded: dict[str, str] = field(default_factory=dict)
    dry_run: bool = False


def s2_id_for(paper: Paper) -> str | None:
    """S2 batch 用的 id（`ARXIV:xxx` / `DOI:xxx`）。都没有则返回 None（跳过）。"""
    if paper.arxiv_id:
        return f"ARXIV:{paper.arxiv_id}"
    if paper.doi:
        return f"DOI:{paper.doi}"
    return None


def canonical_for_ref(ref: S2Reference) -> str | None:
    """被引条目 → 库内主键（与采集侧同一套归一化规则）。"""
    if ref.arxiv_id:
        return paper_id_for_arxiv(ref.arxiv_id)
    if ref.doi:
        return paper_id_for_doi(ref.doi)
    return None


def select_candidates(session: Session, *, limit: int, force: bool = False) -> list[Paper]:
    """取待补查的论文。★ 非 force 只处理 `enrich_state = pending`。"""
    statement = select(Paper)
    if not force:
        statement = statement.where(Paper.enrich_state == ENRICH_PENDING)
    statement = statement.order_by(desc(col(Paper.first_seen_at)))
    rows = list(session.exec(statement).all())
    return rows[:limit] if limit > 0 else rows


def _write_edge(session: Session, src: str, dst: str) -> bool:
    """写一条出边（已存在则跳过）。返回是否**新增**。"""
    key = (src, dst, LINK_TYPE_REFERENCES)
    if session.get(PaperLink, key) is not None:
        return False
    session.add(
        PaperLink(
            src_id=src,
            dst_id=dst,
            link_type=LINK_TYPE_REFERENCES,
            weight=None,
            evidence=EVIDENCE_S2_REFERENCES,
        )
    )
    return True


async def run_enrich(
    *,
    limit: int | None = None,
    force: bool = False,
    dry_run: bool = False,
    settings: Settings | None = None,
    db_path: Path | None = None,
    client: HttpClient | None = None,
) -> EnrichReport:
    """跑一次引用补齐。★ 单源失败只记 `degraded`，**不影响退出码**。"""
    settings = settings or load_settings()
    report = EnrichReport(dry_run=dry_run)
    report.run_id = uuid.uuid4().hex[:12]

    db_file = Path(db_path) if db_path else default_db_path(settings)
    engine = create_engine(f"sqlite:///{db_file}", echo=False)
    SQLModel.metadata.create_all(engine)

    effective_limit = limit if limit is not None else settings.enrich.default_limit
    max_attempts = settings.enrich.max_attempts

    cfg = settings.source(S2_SOURCE_NAME)
    endpoint = (
        cfg.endpoint if cfg else ""
    ) or "https://api.semanticscholar.org/graph/v1/paper/batch"
    api_key = (cfg.api_key if cfg else "") or ""
    batch_size = cfg.batch_size if cfg else 500
    rate_limit = cfg.rate_limit if cfg else "1/1s"

    with Session(engine) as session:
        candidates = select_candidates(session, limit=effective_limit, force=force)
        id_to_paper: dict[str, Paper] = {}
        ordered_ids: list[str] = []
        for paper in candidates:
            s2_id = s2_id_for(paper)
            if s2_id and s2_id not in id_to_paper:
                id_to_paper[s2_id] = paper
                ordered_ids.append(s2_id)
        report.requested = len(candidates)

        if not ordered_ids:
            if not dry_run:
                session.commit()
            return report

        records: list[S2Record | None] = []
        owns_client = client is None
        http = client or HttpClient(user_agent=settings.http.user_agent)
        try:
            records = await fetch_s2_papers(
                http,
                ordered_ids,
                endpoint=endpoint,
                api_key=api_key,
                batch_size=batch_size,
                rate_limit=rate_limit,
            )
        except (SourceError, ValueError) as exc:
            # ★ 整批失败：全部记为一次尝试（仍是 warning，不抛给 CLI）
            report.degraded[S2_SOURCE_NAME] = f"{type(exc).__name__}: {exc}"
            records = []
        finally:
            if owns_client:
                await http.aclose()

        by_id: dict[str, S2Record | None] = {}
        if records and len(records) == len(ordered_ids):
            by_id = dict(zip(ordered_ids, records, strict=False))
        elif records:
            report.degraded[S2_SOURCE_NAME] = "S2 返回条数与请求数不一致（跳过本次写边）"

        cited: set[str] = set()
        for s2_id in ordered_ids:
            paper = id_to_paper[s2_id]
            record = by_id.get(s2_id)

            refs_seen = 0
            in_library = 0
            new_links = 0
            resolved = False
            if record is not None and record.paper_id:
                resolved = True
                report.fetched += 1
                if record.citation_count is not None:
                    paper.citation_count = record.citation_count
                if record.reference_count is not None:
                    paper.refs_count = record.reference_count
                for ref in record.references:
                    refs_seen += 1
                    dst = canonical_for_ref(ref)
                    if not dst or dst == paper.id:
                        continue  # ★ 自引 / 无法归一化 → 跳过
                    if session.get(Paper, dst) is None:
                        continue  # ★ 库外目标：不写边（只计入 refs_count）
                    in_library += 1
                    cited.add(dst)
                    if dry_run:
                        new_links += 1  # 只统计，不落库
                    elif _write_edge(session, paper.id, dst):
                        new_links += 1

            report.refs_seen += refs_seen
            report.in_library_hits += in_library
            report.out_links += new_links

            has_refs = refs_seen > 0 or (record.reference_count or 0 if record else 0) > 0
            if resolved and has_refs:
                paper.enrich_state = ENRICH_DONE
                paper.enrich_error = None
                report.done += 1
            else:
                paper.enrich_attempts += 1
                if resolved:
                    reason = "S2 已索引但 references 为空"
                elif by_id:
                    reason = "S2 未索引该 id"
                else:
                    reason = "S2 请求失败（未获得响应）"
                if paper.enrich_attempts >= max_attempts:
                    paper.enrich_state = ENRICH_STALE
                    report.stale += 1
                else:
                    paper.enrich_state = ENRICH_PENDING
                    report.pending += 1
                paper.enrich_error = f"{reason}（尝试 {paper.enrich_attempts}/{max_attempts} 次）"
            paper.enrich_last_at = utcnow().isoformat(timespec="seconds")

        report.cited_papers = len(cited)
        if not dry_run:
            session.commit()

    return report
