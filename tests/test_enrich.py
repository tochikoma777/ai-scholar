"""L1-A：S2 关联（方向语义 / 只存库内目标 / 补齐队列 / 双轨渲染）。

★ 本阶段最容易犯的错是**方向建模错误**：它能通过所有测试，只是结果反了。
所以这里显式钉死三组：出边 / 入边 / 两者互为反向。

★ fixture 里的响应结构取自 **2026-10-23 真实响应**（完整摘录见 `docs/数据源手册.md`）。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from sqlmodel import Session, SQLModel, select
from typer.testing import CliRunner

from ai_scholar.cli import app
from ai_scholar.collectors.s2 import fetch_s2_papers, parse_batch
from ai_scholar.config import Settings, load_settings
from ai_scholar.export.base import LinkRef
from ai_scholar.export.obsidian import render_link_section
from ai_scholar.pipeline.enrich import run_enrich
from ai_scholar.pipeline.export import run_export
from ai_scholar.store.db import engine_for
from ai_scholar.store.models import (
    ENRICH_DONE,
    ENRICH_PENDING,
    ENRICH_STALE,
    LINK_TYPE_REFERENCES,
    Paper,
    PaperLink,
)
from ai_scholar.utils.http import HttpClient

S2_URL = "https://s2.test/graph/v1/paper/batch"

runner = CliRunner()

# ★ 真实响应结构（未编造）：references 只带 paperId + externalIds；未命中的 id 返回 null
REAL_S2_PAYLOAD = [
    {
        "paperId": "c1f7401acfc4995d0787fa6f961a7ae4802719c9",
        "externalIds": {"ArXiv": "2610.30122", "CorpusId": 293856663},
        "title": "A Quasipolynomial Algorithm",
        "year": 2026,
        "referenceCount": 43,
        "references": [
            {
                "paperId": "b218d7814e8f2be6bcb510429d0ff5cd48b08bb2",
                "externalIds": {"ArXiv": "2610.22149", "CorpusId": 293003685},
            }
        ],
    },
    None,
]


def make_paper(
    pid: str,
    arxiv: str,
    title: str,
    *,
    published: str = "2026-10-20T00:00:00Z",
) -> Paper:
    return Paper(
        id=pid,
        source="arxiv",
        primary_source="arxiv",
        first_seen_at="2026-10-22",
        published_at=published,
        title=title,
        abstract="abstract",
        authors_json="[]",
        venue=None,
        url=f"https://arxiv.org/abs/{arxiv}",
        arxiv_id=arxiv,
        doi=None,
        primary_category="cs.AI",
        categories_json="[]",
        content_hash=f"h-{pid}",
        selected=True,  # ★ 导出相关用例需要 selected=1（export 默认只导 selected）
    )


def seed(db_path: Path, papers: list[Paper]) -> None:
    engine = engine_for(db_path)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        for paper in papers:
            session.add(paper)
        session.commit()


def s2_record(arxiv: str, refs: list[str], *, ref_count: int | None = None) -> dict:
    """构造一条 S2 记录（结构取自真实响应）。"""
    return {
        "paperId": f"s2-{arxiv}",
        "externalIds": {"ArXiv": arxiv},
        "referenceCount": ref_count if ref_count is not None else len(refs),
        "references": [{"paperId": f"r-{r}", "externalIds": {"ArXiv": r}} for r in refs],
    }


def mock_s2(mapping: dict[str, dict | None]) -> object:
    """按请求的 ids 逐条返回（★ 保证响应条数与请求条数一致）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        ids = json.loads(request.content)["ids"]
        return httpx.Response(200, json=[mapping.get(i) for i in ids])

    return respx.post(S2_URL).mock(side_effect=handler)


def settings_for(tmp_path: Path, *, max_attempts: int = 30) -> Settings:
    # ★ 必须是 load_settings()（读 config/default.yaml）：Settings() 不带 sources
    settings = load_settings()
    settings.app.data_dir = str(tmp_path / "data")
    settings.export.links_max_per_section = 10
    settings.enrich.max_attempts = max_attempts
    cfg = settings.source("semantic_scholar")
    assert cfg is not None
    cfg.endpoint = S2_URL
    cfg.api_key = ""
    return settings


# ── ① S2 采集器 ─────────────────────────────────────────────────────────
def test_parse_batch_keeps_positions() -> None:
    records = parse_batch(REAL_S2_PAYLOAD)
    assert len(records) == 2
    first = records[0]
    assert first is not None
    assert first.paper_id == "c1f7401acfc4995d0787fa6f961a7ae4802719c9"
    assert first.arxiv_id == "2610.30122"
    assert first.reference_count == 43
    assert [r.arxiv_id for r in first.references] == ["2610.22149"]
    assert records[1] is None  # ★ 未命中的 id 是 null，位置必须对齐


def test_parse_batch_rejects_non_array() -> None:
    with pytest.raises(ValueError):
        parse_batch({"error": "nope"})


@respx.mock
async def test_api_key_header_only_when_configured() -> None:
    route = respx.post(S2_URL).mock(
        side_effect=[httpx.Response(200, json=[None]), httpx.Response(200, json=[None])]
    )
    async with HttpClient() as client:
        await fetch_s2_papers(client, ["ARXIV:1"], endpoint=S2_URL)
        await fetch_s2_papers(client, ["ARXIV:1"], endpoint=S2_URL, api_key="sk-test")
    assert "x-api-key" not in route.calls[0].request.headers  # ★ 无 key 不发头
    assert route.calls[1].request.headers["x-api-key"] == "sk-test"


@respx.mock
async def test_batch_size_halves_on_oversize_response() -> None:
    """★ 响应超限（413）自动降半重试：250 → 125 → 62。"""

    def handler(request: httpx.Request) -> httpx.Response:
        ids = json.loads(request.content)["ids"]
        if len(ids) > 100:
            return httpx.Response(413)
        return httpx.Response(200, json=[None] * len(ids))

    route = respx.post(S2_URL).mock(side_effect=handler)
    ids = [f"ARXIV:{i}" for i in range(250)]
    async with HttpClient() as client:
        records = await fetch_s2_papers(client, ids, endpoint=S2_URL, batch_size=250)

    sizes = [len(json.loads(call.request.content)["ids"]) for call in route.calls]
    assert sizes[:3] == [250, 125, 62]  # ★ 降半序列
    assert len(records) == len(ids)


# ── ② 方向语义（★ 本阶段头号风险）────────────────────────────────────────
@respx.mock
async def test_out_edges_are_what_the_paper_cites(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "t.db"
    seed(db_path, [make_paper("arXiv:1", "1", "Paper A"), make_paper("arXiv:2", "2", "Paper B")])
    mock_s2({"ARXIV:1": s2_record("1", ["2", "9999"]), "ARXIV:2": None})
    report = await run_enrich(settings=settings_for(tmp_path), db_path=db_path, limit=10)

    with Session(engine_for(db_path)) as session:
        out = list(session.exec(select(PaperLink).where(PaperLink.src_id == "arXiv:1")).all())
        assert [row.dst_id for row in out] == ["arXiv:2"]  # ★ A 引用了 B
        assert out[0].link_type == LINK_TYPE_REFERENCES
        # ★ 库外目标不写边（否则是无法渲染的幽灵条目）
        assert "arXiv:9999" not in {row.dst_id for row in out}
        # 但 refs_count 记录的是**全部** references
        assert session.get(Paper, "arXiv:1").refs_count == 2

    assert report.refs_seen == 2
    assert report.in_library_hits == 1
    assert report.out_links == 1


@respx.mock
async def test_in_edges_are_reverse_lookups(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "t.db"
    seed(db_path, [make_paper("arXiv:1", "1", "Paper A"), make_paper("arXiv:2", "2", "Paper B")])
    mock_s2({"ARXIV:1": s2_record("1", ["2"]), "ARXIV:2": None})
    await run_enrich(settings=settings_for(tmp_path), db_path=db_path, limit=10)

    with Session(engine_for(db_path)) as session:
        inbound = list(session.exec(select(PaperLink).where(PaperLink.dst_id == "arXiv:2")).all())
        assert [row.src_id for row in inbound] == ["arXiv:1"]  # ★ 谁引用了 B
        # ★ 入边不单独存储：表里只有一行
        assert len(session.exec(select(PaperLink)).all()) == 1


@respx.mock
async def test_out_and_in_edges_are_mutually_reverse(tmp_path: Path) -> None:
    """★ 互为反向：每条 (src,dst)，从 dst 侧查入边必然查回 src。"""
    db_path = tmp_path / "data" / "t.db"
    seed(
        db_path,
        [
            make_paper("arXiv:1", "1", "Paper A"),
            make_paper("arXiv:2", "2", "Paper B"),
            make_paper("arXiv:3", "3", "Paper C"),
        ],
    )
    mock_s2(
        {
            "ARXIV:1": s2_record("1", ["2", "3"]),  # A 引用 B、C
            "ARXIV:2": None,
            "ARXIV:3": s2_record("3", ["1"]),  # C 引用 A（让 A 也有入边）
        }
    )
    await run_enrich(settings=settings_for(tmp_path), db_path=db_path, limit=10)

    with Session(engine_for(db_path)) as session:
        edges = list(session.exec(select(PaperLink)).all())
        assert {(e.src_id, e.dst_id) for e in edges} == {
            ("arXiv:1", "arXiv:2"),
            ("arXiv:1", "arXiv:3"),
            ("arXiv:3", "arXiv:1"),
        }
        for edge in edges:
            back = list(
                session.exec(
                    select(PaperLink).where(
                        PaperLink.dst_id == edge.dst_id,
                        PaperLink.src_id == edge.src_id,
                    )
                ).all()
            )
            assert len(back) == 1 and back[0].src_id == edge.src_id
        assert {
            row.src_id
            for row in session.exec(select(PaperLink).where(PaperLink.dst_id == "arXiv:1"))
        } == {"arXiv:3"}


# ── ③ 补齐队列 ──────────────────────────────────────────────────────────
@respx.mock
async def test_unresolved_paper_retries_then_goes_stale(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "t.db"
    seed(db_path, [make_paper("arXiv:1", "1", "Paper A")])
    route = mock_s2({"ARXIV:1": None})
    settings = settings_for(tmp_path, max_attempts=2)

    await run_enrich(settings=settings, db_path=db_path, limit=10)
    with Session(engine_for(db_path)) as session:
        paper = session.get(Paper, "arXiv:1")
        assert paper.enrich_state == ENRICH_PENDING
        assert paper.enrich_attempts == 1

    await run_enrich(settings=settings, db_path=db_path, limit=10)
    with Session(engine_for(db_path)) as session:
        paper = session.get(Paper, "arXiv:1")
        assert paper.enrich_state == ENRICH_STALE  # ★ 超过 max_attempts 停止重试
        assert paper.enrich_error and "未索引" in paper.enrich_error
    assert route.call_count == 2


@respx.mock
async def test_empty_references_stays_pending(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "t.db"
    seed(db_path, [make_paper("arXiv:1", "1", "Paper A")])
    mock_s2({"ARXIV:1": s2_record("1", [])})
    await run_enrich(settings=settings_for(tmp_path), db_path=db_path, limit=10)
    with Session(engine_for(db_path)) as session:
        paper = session.get(Paper, "arXiv:1")
        assert paper.enrich_state == ENRICH_PENDING  # ★ 空也要重试
        assert paper.refs_count == 0


@respx.mock
async def test_resolved_paper_is_done_and_skipped_next_run(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "t.db"
    seed(db_path, [make_paper("arXiv:1", "1", "Paper A")])
    mock_s2({"ARXIV:1": s2_record("1", ["9999"], ref_count=5)})
    settings = settings_for(tmp_path)
    report = await run_enrich(settings=settings, db_path=db_path, limit=10)
    assert report.done == 1
    with Session(engine_for(db_path)) as session:
        assert session.get(Paper, "arXiv:1").enrich_state == ENRICH_DONE

    # ★ 第二次默认只处理 pending ⇒ 不再请求 S2
    second = await run_enrich(settings=settings, db_path=db_path, limit=10)
    assert second.requested == 0


@respx.mock
async def test_s2_failure_is_degraded_not_fatal(tmp_path: Path) -> None:
    """★ 单源失败不影响退出码：这里只记 degraded，attempts 照常累加。"""
    db_path = tmp_path / "data" / "t.db"
    seed(db_path, [make_paper("arXiv:1", "1", "Paper A")])
    respx.post(S2_URL).mock(return_value=httpx.Response(429))
    report = await run_enrich(settings=settings_for(tmp_path), db_path=db_path, limit=10)
    assert "semantic_scholar" in report.degraded
    assert report.fetched == 0
    with Session(engine_for(db_path)) as session:
        assert session.get(Paper, "arXiv:1").enrich_attempts == 1


# ── ④ 双轨渲染 ──────────────────────────────────────────────────────────
def test_render_link_section_dual_track() -> None:
    refs = [
        LinkRef("arXiv:1", "T1", "https://x/1", published_at="2026-01-01"),
        LinkRef("arXiv:2", "T2", "https://x/2", published_at="2026-02-01"),
    ]
    note_index = {"arXiv:1": "2026-01-01__T1"}
    lines = render_link_section("引用了", refs, note_index, max_items=1)
    assert lines[0] == "### 引用了"
    assert lines[1].startswith("- [[2026-01-01__T1]]")  # ★ 有笔记的排前
    assert "还有 1 篇" in lines[2]  # ★ 超出上限要说明

    no_note = render_link_section("引用了", [refs[1]], {}, max_items=5)
    assert no_note[1] == "- ★ T2（无笔记）— https://x/2"  # ★ 不建空笔记


def test_render_link_section_empty_state_is_explicit() -> None:
    lines = render_link_section("被引用", [], {}, empty_hint="（暂无 X）")
    assert lines == ["### 被引用", "（暂无 X）", ""]


def test_export_renders_real_links_and_backfills_note_path(tmp_path: Path) -> None:
    """★ 双轨渲染接真实数据：互为反向的两篇都渲染出对方，且不建空文件。"""
    db_path = tmp_path / "data" / "t.db"
    seed(
        db_path,
        [
            make_paper("arXiv:1", "1", "Paper A"),
            make_paper("arXiv:2", "2", "Paper B"),
        ],
    )
    engine = engine_for(db_path)
    with Session(engine) as session:
        session.add(
            PaperLink(
                src_id="arXiv:1",
                dst_id="arXiv:2",
                link_type=LINK_TYPE_REFERENCES,
                evidence="s2_references",
            )
        )
        session.commit()

    settings = settings_for(tmp_path)
    vault = tmp_path / "vault"
    run_export(settings=settings, db_path=db_path, out=vault, limit=2)

    notes = sorted((vault / "10-文献").rglob("*.md"))
    assert len(notes) == 2
    text_a = next(p for p in notes if "Paper-A" in p.name).read_text(encoding="utf-8")
    text_b = next(p for p in notes if "Paper-B" in p.name).read_text(encoding="utf-8")
    assert "### 引用了" in text_a and "- [[2026-10-22__Paper-B-" in text_a
    assert "### 被引用" in text_b and "- [[2026-10-22__Paper-A-" in text_b

    with Session(engine) as session:
        assert session.get(Paper, "arXiv:1").note_path is not None  # ★ 回填
        assert session.get(Paper, "arXiv:2").note_path is not None

    # ★ 幂等：二次导出无变更（新渲染不能破坏 --check）
    assert run_export(settings=settings, db_path=db_path, out=vault, check=True).changed == []


def test_link_to_note_from_previous_export_uses_wikilink(tmp_path: Path) -> None:
    """★ note_path 的价值：目标不在本次选集、但上次已导出过 ⇒ 仍是 [[双链]]。"""
    db_path = tmp_path / "data" / "t.db"
    seed(
        db_path,
        [
            make_paper("arXiv:1", "1", "Paper A"),
            make_paper("arXiv:2", "2", "Paper B"),
        ],
    )
    engine = engine_for(db_path)
    with Session(engine) as session:
        session.add(
            PaperLink(
                src_id="arXiv:1",
                dst_id="arXiv:2",
                link_type=LINK_TYPE_REFERENCES,
                evidence="s2_references",
            )
        )
        session.commit()

    settings = settings_for(tmp_path)
    vault = tmp_path / "vault"
    run_export(settings=settings, db_path=db_path, out=vault, ids=["arXiv:2"])
    # 只导出 A：B 的笔记不在本次选集，但 db 里有 note_path ⇒ 仍应渲染双链
    run_export(settings=settings, db_path=db_path, out=vault, ids=["arXiv:1"])
    note_a = next((vault / "10-文献").rglob("*Paper-A*.md"))
    text = note_a.read_text(encoding="utf-8")
    assert "- [[2026-10-22__Paper-B-" in text
    assert "（无笔记）" not in text


def test_related_cli_has_two_columns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """★ `related` 分两栏：左边"引用了"、右边"被引用"（人工验证方向用）。"""
    root = tmp_path / "repo"
    monkeypatch.setenv("AI_SCHOLAR_ROOT", str(root))
    db_path = root / "data" / "ai_scholar.db"
    seed(
        db_path,
        [
            make_paper("arXiv:1", "1", "Paper A"),
            make_paper("arXiv:2", "2", "Paper B"),
            make_paper("arXiv:3", "3", "Paper C"),
        ],
    )
    engine = engine_for(db_path)
    with Session(engine) as session:
        session.add(PaperLink(src_id="arXiv:1", dst_id="arXiv:2", link_type=LINK_TYPE_REFERENCES))
        session.add(PaperLink(src_id="arXiv:3", dst_id="arXiv:1", link_type=LINK_TYPE_REFERENCES))
        session.commit()

    result = runner.invoke(app, ["related", "arXiv:1"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    body = result.output
    assert "引用了" in body and "被引用" in body
    # ★ 断言真实表格行：同一行的左栏是它引用的、右栏是引用它的
    row = next(line for line in body.splitlines() if "Paper B" in line)
    assert "Paper C" in row
    assert body.index("引用了") < body.index("被引用")
