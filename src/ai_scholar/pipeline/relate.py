"""关联计算（L1-C）：文献耦合 + TF-IDF 语义相似。★ 零 LLM、零额外网络请求。

**文献耦合**（§5.1c）：A、B 引用了同一批文献 ⇒ 相关。**不需要被引论文在库内** ——
引用边命中率只有 0.0095%，耦合才是冷启动期可用的引用类关联。

★ **禁止全库两两比较**：必须按 `ref_external_id` 分桶（`idx_refs_external`），
只比较同桶内的论文对。2174 篇 → 236 万对还能忍，23 万篇 → 264 亿对直接崩。

**语义相似**（§5.2）：title×2 + abstract，TF-IDF + 余弦；**增量**：
只算"尚未比对过（`similar_at IS NULL`）的论文 vs 存量"，写对称边后老论文也能看到新邻居。

对称边只存一条：`src_id < dst_id`（字典序），渲染时双向取 —— 避免双倍行数与两套真相。
"""

from __future__ import annotations

import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine, select

from ai_scholar.config import Settings, load_settings
from ai_scholar.store.db import default_db_path
from ai_scholar.store.models import (
    LINK_TYPE_BIBCOUPLING,
    LINK_TYPE_SIMILARITY,
    Paper,
    PaperLink,
    PaperReference,
)
from ai_scholar.utils.timeutil import today_local

KIND_BIBCOUPLING = "bibcoupling"
KIND_SIMILARITY = "similarity"
KINDS = (KIND_BIBCOUPLING, KIND_SIMILARITY)


@dataclass
class RelateReport:
    run_id: str = ""
    kind: str = "all"
    papers: int = 0  # 参与计算的论文数
    ref_rows: int = 0  # 读取的引用行数
    buckets: int = 0  # 含 ≥2 篇论文的倒排桶数
    pairs_compared: int = 0  # ★ Σ C(n,2)：与总篇数平方无关
    avg_cooccur: float = 0.0  # 同桶对的平均共现引用数（小规模验证用）
    edges_new: dict[str, int] = field(default_factory=dict)
    edges_existing: dict[str, int] = field(default_factory=dict)
    docs: int = 0  # 语义相似本次比对的文档数
    degraded: dict[str, str] = field(default_factory=dict)
    dry_run: bool = False
    duration_s: float = 0.0


def load_reference_index(
    session: Session, paper_ids: list[str] | None = None
) -> tuple[dict[str, set[str]], int]:
    """读引用列表并按**被引 id** 建倒排所需的 `refs_by_paper`。"""
    statement = select(PaperReference)
    if paper_ids is not None:
        allowed = set(paper_ids)
        rows = [row for row in session.exec(statement).all() if row.paper_id in allowed]
    else:
        rows = list(session.exec(statement).all())

    refs_by_paper: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        refs_by_paper[row.paper_id].add(row.ref_external_id)
    return refs_by_paper, len(rows)


def bibcoupling_pairs(
    refs_by_paper: dict[str, set[str]],
) -> tuple[dict[tuple[str, str], int], int, int]:
    """倒排 + 桶内两两计数。返回 `(pair_counts, pairs_compared, buckets)`。

    ★ `pairs_compared = Σ C(n_bucket, 2)`：只与**同桶论文数**有关，与全库规模无关。
    """
    inverted: dict[str, list[str]] = defaultdict(list)
    for pid, refs in refs_by_paper.items():
        for ref in refs:
            inverted[ref].append(pid)

    pair_counts: dict[tuple[str, str], int] = defaultdict(int)
    pairs_compared = 0
    buckets = 0
    for pids in inverted.values():
        if len(pids) < 2:
            continue
        buckets += 1
        pids.sort()
        for i in range(len(pids)):
            for j in range(i + 1, len(pids)):
                pair_counts[(pids[i], pids[j])] += 1
                pairs_compared += 1
    return dict(pair_counts), pairs_compared, buckets


def rank_bibcoupling(
    pair_counts: dict[tuple[str, str], int],
    refs_by_paper: dict[str, set[str]],
    *,
    top_n: int,
    min_weight: float,
) -> dict[tuple[str, str], tuple[float, int]]:
    """Jaccard 归一化 + 每篇取 Top-N。返回 `{(a,b): (weight, cooccur)}`（a<b）。"""
    by_paper: dict[str, list[tuple[float, int, str]]] = defaultdict(list)
    for (a, b), cooccur in pair_counts.items():
        union = len(refs_by_paper[a]) + len(refs_by_paper[b]) - cooccur
        weight = cooccur / union if union else 0.0
        if weight < min_weight:
            continue
        by_paper[a].append((weight, cooccur, b))
        by_paper[b].append((weight, cooccur, a))

    accepted: dict[tuple[str, str], tuple[float, int]] = {}
    for pid, candidates in by_paper.items():
        candidates.sort(reverse=True)
        for weight, cooccur, other in candidates[:top_n]:
            key = (min(pid, other), max(pid, other))
            accepted[key] = (weight, cooccur)
    return accepted


def _upsert_edge(
    session: Session,
    a: str,
    b: str,
    *,
    link_type: str,
    weight: float,
    evidence: str,
) -> bool:
    """写一条对称边（a<b）。返回是否新增。"""
    row = session.get(PaperLink, (a, b, link_type))
    if row is not None:
        changed = row.weight != weight or row.evidence != evidence
        if changed:
            row.weight = weight
            row.evidence = evidence
        return False
    session.add(
        PaperLink(
            src_id=a,
            dst_id=b,
            link_type=link_type,
            weight=weight,
            evidence=evidence,
        )
    )
    return True


def build_similarity_text(paper: Paper, *, title_weight: float) -> str:
    title = " ".join((paper.title or "").split())
    abstract = " ".join((paper.abstract or "").split())
    # ★ 标题 ×2 + 摘要 ×1（§5.2）；用重复近似权重，不引入额外依赖
    return " ".join([title] * max(1, int(round(title_weight))) + [abstract])


def similarity_edges(
    papers: list[Paper],
    pending: list[Paper],
    *,
    top_k: int,
    min_weight: float,
    title_weight: float,
    max_features: int,
) -> dict[tuple[str, str], float]:
    """TF-IDF 余弦（★ 只算 pending vs 全量，不做全量重算）。sklearn 缺失时抛 ImportError。"""
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity

    texts = [build_similarity_text(p, title_weight=title_weight) for p in papers]
    vectorizer = TfidfVectorizer(
        stop_words="english",
        ngram_range=(1, 2),
        max_features=max_features,
    )
    matrix = vectorizer.fit_transform(texts)
    index_of = {p.id: i for i, p in enumerate(papers)}

    edges: dict[tuple[str, str], float] = {}
    for paper in pending:
        i = index_of[paper.id]
        sims = cosine_similarity(matrix[i], matrix).ravel()
        order = sims.argsort()[::-1]
        picked = 0
        for j in order:
            if j == i:
                continue
            weight = float(sims[j])
            if weight < min_weight:
                break  # 已按降序，后面只会更低
            other = papers[int(j)].id
            key = (min(paper.id, other), max(paper.id, other))
            edges[key] = max(weight, edges.get(key, 0.0))
            picked += 1
            if picked >= top_k:
                break
    return edges


def run_relate(
    *,
    kind: str = "all",
    limit: int | None = None,
    dry_run: bool = False,
    today: date | None = None,
    settings: Settings | None = None,
    db_path: Path | None = None,
) -> RelateReport:
    """跑一次关联计算。★ 失败只记 degraded，不影响退出码；零 LLM。"""
    started = time.monotonic()
    settings = settings or load_settings()
    report = RelateReport(run_id=uuid.uuid4().hex[:12], kind=kind, dry_run=dry_run)
    day = today or today_local()

    db_file = Path(db_path) if db_path else default_db_path(settings)
    engine = create_engine(f"sqlite:///{db_file}", echo=False)
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        papers = list(session.exec(select(Paper)).all())
        by_id = {p.id: p for p in papers}

        considered = papers
        if limit is not None and limit > 0:
            considered = sorted(papers, key=lambda p: p.first_seen_at or "", reverse=True)[:limit]
        report.papers = len(considered)
        considered_ids = [p.id for p in considered]

        if kind in ("all", KIND_BIBCOUPLING):
            refs_by_paper, ref_rows = load_reference_index(session, considered_ids)
            report.ref_rows = ref_rows
            pair_counts, pairs_compared, buckets = bibcoupling_pairs(refs_by_paper)
            report.pairs_compared = pairs_compared
            report.buckets = buckets
            if pair_counts:
                report.avg_cooccur = round(sum(pair_counts.values()) / len(pair_counts), 2)
            accepted = rank_bibcoupling(
                pair_counts,
                refs_by_paper,
                top_n=settings.relate.bibcoupling_top_n,
                min_weight=settings.relate.bibcoupling_min_weight,
            )
            new = existing = 0
            for (a, b), (weight, cooccur) in accepted.items():
                if a not in by_id or b not in by_id:
                    continue
                evidence = f"共现 {cooccur} 篇参考文献"
                if dry_run:
                    existing += 0
                    new += 1
                elif _upsert_edge(
                    session,
                    a,
                    b,
                    link_type=LINK_TYPE_BIBCOUPLING,
                    weight=round(weight, 4),
                    evidence=evidence,
                ):
                    new += 1
                else:
                    existing += 1
            report.edges_new[KIND_BIBCOUPLING] = new
            report.edges_existing[KIND_BIBCOUPLING] = existing

        if kind in ("all", KIND_SIMILARITY):
            pending = [p for p in papers if p.similar_at is None]
            if limit is not None and limit > 0:
                pending = pending[:limit]
            report.docs = len(pending)
            if pending:
                try:
                    edges = similarity_edges(
                        papers,
                        pending,
                        top_k=settings.relate.similarity_top_k,
                        min_weight=settings.relate.similarity_min_weight,
                        title_weight=settings.relate.similarity_title_weight,
                        max_features=settings.relate.similarity_max_features,
                    )
                except ImportError:
                    report.degraded[KIND_SIMILARITY] = "scikit-learn 未安装，已跳过"
                else:
                    new = existing = 0
                    for (a, b), weight in edges.items():
                        if dry_run or _upsert_edge(
                            session,
                            a,
                            b,
                            link_type=LINK_TYPE_SIMILARITY,
                            weight=round(weight, 4),
                            evidence=f"相似 {weight:.2f}",
                        ):
                            new += 1
                        else:
                            existing += 1
                    report.edges_new[KIND_SIMILARITY] = new
                    report.edges_existing[KIND_SIMILARITY] = existing
                    if not dry_run:
                        for paper in pending:
                            paper.similar_at = day.isoformat()

        if not dry_run:
            session.commit()

    report.duration_s = round(time.monotonic() - started, 2)
    return report
