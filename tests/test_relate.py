"""L1-C：文献耦合（倒排索引）+ TF-IDF 相似 + 双轨渲染。

★ 本阶段最容易犯的两个错：
1. **全库两两比较** —— 必须按 `ref_external_id` 分桶（这里用 `pairs_compared` 钉死）
2. **把对称边当引用渲染** —— 耦合/相似必须走独立分节，不能混进「引用了」
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import respx
from sqlmodel import Session, SQLModel, select
from typer.testing import CliRunner

from ai_scholar.cli import app
from ai_scholar.collectors.s2 import S2Reference
from ai_scholar.config import load_settings
from ai_scholar.export.base import ExportContext, LinkRef
from ai_scholar.export.obsidian import ObsidianExporter
from ai_scholar.pipeline.enrich import external_id_for_ref, run_enrich
from ai_scholar.pipeline.export import run_export
from ai_scholar.pipeline.relate import (
    bibcoupling_pairs,
    rank_bibcoupling,
    run_relate,
)
from ai_scholar.store.db import engine_for
from ai_scholar.store.models import (
    LINK_TYPE_BIBCOUPLING,
    LINK_TYPE_SIMILARITY,
    Paper,
    PaperLink,
    PaperReference,
)

S2_URL = "https://s2.test/graph/v1/paper/batch"
runner = CliRunner()


def make_paper(
    pid: str,
    title: str = "A Study of Things",
    abstract: str = "abstract text about things and methods",
    *,
    arxiv: str | None = None,
    selected: bool = True,
) -> Paper:
    return Paper(
        id=pid,
        source="arxiv",
        primary_source="arxiv",
        first_seen_at="2026-10-20",
        published_at="2026-10-18T12:00:00Z",
        title=title,
        abstract=abstract,
        authors_json="[]",
        venue=None,
        url=f"https://arxiv.org/abs/{arxiv}" if arxiv else None,
        arxiv_id=arxiv or pid.split(":", 1)[-1],
        doi=None,
        primary_category="cs.AI",
        categories_json="[]",
        content_hash=f"h-{pid}",
        selected=selected,
    )


def seed(db_path: Path, papers: list[Paper], refs: dict[str, list[str]] | None = None) -> None:
    engine = engine_for(db_path)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        for paper in papers:
            session.add(paper)
        for pid, items in (refs or {}).items():
            for ref in items:
                session.add(PaperReference(paper_id=pid, ref_external_id=ref))
        session.commit()


def settings_for(tmp_path: Path) -> object:
    settings = load_settings()
    settings.app.data_dir = str(tmp_path / "data")
    return settings


# ── ① 引用列表 ──────────────────────────────────────────────────────────
def test_external_id_priority() -> None:
    assert external_id_for_ref(S2Reference(arxiv_id="2401.12345v2")) == "ARXIV:2401.12345"
    assert external_id_for_ref(S2Reference(doi="10.1000/ABC")) == "DOI:10.1000/abc"
    assert external_id_for_ref(S2Reference(paper_id="abc123")) == "S2:abc123"
    assert external_id_for_ref(S2Reference()) is None


@respx.mock
async def test_enrich_stores_references_idempotently(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "t.db"
    seed(db_path, [make_paper("arXiv:1", arxiv="1")])
    respx.post(S2_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "paperId": "s2-1",
                    "externalIds": {"ArXiv": "1"},
                    "referenceCount": 2,
                    "references": [
                        {"paperId": "r1", "externalIds": {"ArXiv": "2401.00001"}},
                        {"paperId": "r2", "externalIds": {"DOI": "10.1000/x"}},
                    ],
                }
            ],
        )
    )
    settings = settings_for(tmp_path)
    settings.source("semantic_scholar").endpoint = S2_URL

    first = await run_enrich(settings=settings, db_path=db_path, limit=10)
    assert first.refs_stored == 2
    second = await run_enrich(settings=settings, db_path=db_path, limit=10, force=True)
    assert second.refs_stored == 0  # ★ 幂等

    with Session(engine_for(db_path)) as session:
        rows = list(session.exec(select(PaperReference)).all())
        assert {r.ref_external_id for r in rows} == {"ARXIV:2401.00001", "DOI:10.1000/x"}


# ── ② 倒排索引（★ 禁止全库两两比较）────────────────────────────────────
def test_bibcoupling_uses_buckets_not_full_product() -> None:
    refs = {
        "A": {"r1", "r2"},
        "B": {"r1", "r3"},
        "C": {"r9"},
    }
    pair_counts, pairs_compared, buckets = bibcoupling_pairs(refs)
    assert pair_counts == {("A", "B"): 1}
    assert pairs_compared == 1  # 只有 r1 桶贡献 1 对
    assert buckets == 1
    # ★ 全库两两比较会是 C(3,2)=3；同桶才是 1 —— 规模越大差距越离谱
    assert pairs_compared < 3


def test_bibcoupling_pairs_scale_with_bucket_not_library() -> None:
    refs = {f"P{i}": {"shared"} for i in range(5)}  # 5 篇共享同一条引用
    _, pairs_compared, buckets = bibcoupling_pairs(refs)
    assert buckets == 1
    assert pairs_compared == 10  # C(5,2)，与桶外论文数无关


def test_bibcoupling_jaccard_and_top_n() -> None:
    refs = {
        "A": {"r1", "r2", "r3"},
        "B": {"r1", "r2"},  # cooccur 2 / union 3 = 0.6667
        "C": {"r1"},  # cooccur 1 / union 3 = 0.3333
    }
    pair_counts, _, _ = bibcoupling_pairs(refs)
    accepted = rank_bibcoupling(pair_counts, refs, top_n=1, min_weight=0.1)
    # ★ 每篇 Top-1：A 选 B（0.667），B 也选 A；C 只剩 A
    assert ("A", "B") in accepted
    assert accepted[("A", "B")][1] == 2
    assert ("A", "C") not in accepted or ("A", "B") in accepted


def test_bibcoupling_threshold_filters() -> None:
    refs = {"A": {"r1", "r2", "r3", "r4"}, "B": {"r1"}}  # 1/4 = 0.25
    pair_counts, _, _ = bibcoupling_pairs(refs)
    assert rank_bibcoupling(pair_counts, refs, top_n=10, min_weight=0.5) == {}
    assert ("A", "B") in rank_bibcoupling(pair_counts, refs, top_n=10, min_weight=0.2)


# ── ③ 文献耦合落边 + 幂等 ───────────────────────────────────────────────
def test_run_relate_writes_bibcoupling_edges(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "t.db"
    papers = [make_paper(f"arXiv:{i}", f"Paper {i}") for i in range(3)]
    seed(
        db_path,
        papers,
        {
            "arXiv:0": ["ARXIV:r1", "ARXIV:r2"],
            "arXiv:1": ["ARXIV:r1", "ARXIV:r2"],
            "arXiv:2": ["ARXIV:r9"],
        },
    )
    report = run_relate(kind="bibcoupling", db_path=db_path, settings=settings_for(tmp_path))
    assert report.edges_new[LINK_TYPE_BIBCOUPLING] == 1
    # ★ 两条共享引用 ⇒ 两个桶各贡献 1 对；与"全库两两比较 3 对"无关
    assert report.pairs_compared == 2

    with Session(engine_for(db_path)) as session:
        row = session.exec(
            select(PaperLink).where(PaperLink.link_type == LINK_TYPE_BIBCOUPLING)
        ).one()
        assert (row.src_id, row.dst_id) == ("arXiv:0", "arXiv:1")  # ★ 对称边只存一条
        assert row.weight == 1.0
        assert row.evidence == "共现 2 篇参考文献"

    again = run_relate(kind="bibcoupling", db_path=db_path, settings=settings_for(tmp_path))
    assert again.edges_new[LINK_TYPE_BIBCOUPLING] == 0  # ★ 幂等
    assert again.edges_existing[LINK_TYPE_BIBCOUPLING] == 1


# ── ④ TF-IDF 相似（增量）───────────────────────────────────────────────
def test_similarity_matches_and_is_incremental(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "t.db"
    seed(
        db_path,
        [
            make_paper(
                "arXiv:1",
                "Efficient Transformers for Long Context",
                "We propose efficient attention for long context transformers.",
            ),
            make_paper(
                "arXiv:2",
                "Efficient Transformers for Long Context",
                "We propose efficient attention for long context transformers.",
            ),
            make_paper(
                "arXiv:3",
                "Crystal Structure Prediction",
                "A diffusion model for crystal structure prediction.",
            ),
        ],
    )
    settings = settings_for(tmp_path)
    first = run_relate(kind="similarity", db_path=db_path, settings=settings)
    assert first.edges_new[LINK_TYPE_SIMILARITY] >= 1
    assert first.docs == 3

    with Session(engine_for(db_path)) as session:
        rows = list(
            session.exec(select(PaperLink).where(PaperLink.link_type == LINK_TYPE_SIMILARITY)).all()
        )
        assert rows
        assert all(r.src_id < r.dst_id for r in rows)  # ★ 规范化顺序
        assert any(r.src_id == "arXiv:1" and r.dst_id == "arXiv:2" for r in rows)
        assert all(r.evidence.startswith("相似 ") for r in rows)
        # ★ 增量标记已写
        assert all(session.get(Paper, pid).similar_at for pid in ("arXiv:1", "arXiv:2", "arXiv:3"))

    second = run_relate(kind="similarity", db_path=db_path, settings=settings)
    assert second.docs == 0  # ★ 没有新论文 ⇒ 不重算


def test_similarity_skips_when_sklearn_missing(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "data" / "t.db"
    seed(db_path, [make_paper("arXiv:1"), make_paper("arXiv:2")])
    # ★ 只把顶层包置 None 不够：子模块可能已被缓存，必须一起打掉
    for name in list(sys.modules):
        if name == "sklearn" or name.startswith("sklearn."):
            monkeypatch.setitem(sys.modules, name, None)
    report = run_relate(kind="similarity", db_path=db_path, settings=settings_for(tmp_path))
    assert "scikit-learn" in report.degraded.get(LINK_TYPE_SIMILARITY, "")
    with Session(engine_for(db_path)) as session:
        assert session.get(Paper, "arXiv:1").similar_at is None  # 未标记，下次还会试


# ── ⑤ 双轨渲染：新类型必须独立分节 ──────────────────────────────────────
def test_render_new_link_sections(tmp_path: Path) -> None:
    exporter = ObsidianExporter()
    paper = make_paper("arXiv:1")
    context = ExportContext(
        vault_dir=tmp_path,
        llm_mode="off",
        note_index={"arXiv:2": "2026-10-20__Paper-B"},
        similar=[
            LinkRef(
                "arXiv:2",
                "Paper B",
                None,
                link_type=LINK_TYPE_SIMILARITY,
                weight=0.42,
                evidence="相似 0.42",
            )
        ],
        coupled=[
            LinkRef(
                "arXiv:3",
                "Paper C",
                "https://arxiv.org/abs/3",
                link_type=LINK_TYPE_BIBCOUPLING,
                weight=0.5,
                evidence="共现 4 篇参考文献",
            )
        ],
    )
    text = exporter.render(paper, context)
    assert "### 相似" in text
    assert "- [[2026-10-20__Paper-B]] — 相似 0.42" in text
    assert "### 文献耦合" in text
    assert "- ★ Paper C（无笔记）— https://arxiv.org/abs/3" in text
    # ★ 不能混进「引用了」
    assert text.index("### 引用了") < text.index("### 相似") < text.index("### 文献耦合")


def test_render_omits_empty_new_sections(tmp_path: Path) -> None:
    exporter = ObsidianExporter()
    text = exporter.render(make_paper("arXiv:1"), ExportContext(vault_dir=tmp_path))
    assert "### 相似" not in text
    assert "### 文献耦合" not in text


def test_export_renders_coupling_edge(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "t.db"
    seed(
        db_path,
        [make_paper("arXiv:1", "Paper A"), make_paper("arXiv:2", "Paper B")],
        {
            "arXiv:1": ["ARXIV:r1", "ARXIV:r2"],
            "arXiv:2": ["ARXIV:r1", "ARXIV:r2"],
        },
    )
    settings = settings_for(tmp_path)
    run_relate(kind="bibcoupling", db_path=db_path, settings=settings)
    vault = tmp_path / "vault"
    run_export(settings=settings, db_path=db_path, out=vault)

    note_a = next((vault / "10-文献").rglob("*Paper-A*.md"))
    text = note_a.read_text(encoding="utf-8")
    assert "### 文献耦合" in text
    assert "[[2026-10-20__Paper-B-" in text  # 目标已导出 ⇒ 双链
    # ★ 不建空笔记：只有 2 篇
    assert len(list((vault / "10-文献").rglob("*.md"))) == 2


def test_symmetric_edge_does_not_leak_into_references_section(tmp_path: Path) -> None:
    """★ 对称边（相似/耦合）绝不能出现在「引用了」里 —— 那会把相似误标成引用。"""
    db_path = tmp_path / "data" / "t.db"
    seed(
        db_path,
        [
            make_paper(
                "arXiv:1",
                "Efficient Transformers for Long Context",
                "We propose efficient attention for long context transformers.",
            ),
            make_paper(
                "arXiv:2",
                "Efficient Transformers for Long Context",
                "We propose efficient attention for long context transformers.",
            ),
        ],
    )
    settings = settings_for(tmp_path)
    run_relate(kind="similarity", db_path=db_path, settings=settings)
    vault = tmp_path / "vault"
    run_export(settings=settings, db_path=db_path, out=vault)

    note = next((vault / "10-文献").rglob("*Efficient-Transformers*"))
    text = note.read_text(encoding="utf-8")
    cited = text.split("### 引用了", 1)[1].split("### 被引用", 1)[0]
    assert "相似" not in cited  # ★ 引用区里不得出现相似边
    assert "（暂无 — 引用数据由" in cited
    assert "### 相似" in text


# ── ⑥ CLI ───────────────────────────────────────────────────────────────
def test_relate_cli_reports_bucket_pairs(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    db_path = root / "data" / "ai_scholar.db"
    seed(
        db_path,
        [make_paper("arXiv:1"), make_paper("arXiv:2")],
        {"arXiv:1": ["ARXIV:r1"], "arXiv:2": ["ARXIV:r1"]},
    )
    monkeypatch.setenv("AI_SCHOLAR_ROOT", str(root))
    result = runner.invoke(app, ["relate", "--type", "bibcoupling"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "桶内对数" in result.output
    assert "bibcoupling" in result.output


def test_relate_cli_rejects_unknown_type() -> None:
    result = runner.invoke(app, ["relate", "--type", "nope"])
    assert result.exit_code == 2
