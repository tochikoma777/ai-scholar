"""L0-C：Obsidian 导出器。

★ 本阶段**头号验收项**是幂等：连续两次 `export` 必须**无 diff**。
其余硬约束：标记块不覆盖用户区、frontmatter 读-合并-写回、文件名清洗、
双轨渲染（无笔记不建空文件）、原子写入、`citations` 不每次更新。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import respx
from sqlmodel import Session, SQLModel, select

from ai_scholar.config import Settings
from ai_scholar.export.base import ExportContext, LinkRef
from ai_scholar.export.obsidian import (
    BEGIN_MARKER,
    END_MARKER,
    ObsidianExporter,
    note_name,
    parse_frontmatter,
    render_link_line,
    sanitize_title,
)
from ai_scholar.pipeline.export import run_export
from ai_scholar.store.db import engine_for
from ai_scholar.store.models import Paper, Run


def make_paper(
    pid: str = "arXiv:2610.00001",
    title: str = "A Study of Things: With a Colon",
    *,
    first_seen: str = "2026-10-20",
    published: str = "2026-10-18T12:00:00Z",
    hf_score: int | None = 7,
    citations: int | None = 3,
    category: str = "cs.AI",
) -> Paper:
    return Paper(
        id=pid,
        source="arxiv",
        primary_source="arxiv",
        first_seen_at=first_seen,
        published_at=published,
        title=title,
        abstract="abstract text",
        authors_json=json.dumps(["John Doe", "Jane Roe"], ensure_ascii=False),
        venue=None,
        url="https://arxiv.org/abs/2610.00001",
        arxiv_id=pid.split(":", 1)[-1],
        doi=None,
        primary_category=category,
        categories_json=json.dumps([category]),
        hf_score=hf_score,
        citation_count=citations,
        refs_count=12,
        content_hash="deadbeef",
    )


def _seed_db(db_path: Path, papers: list[Paper]) -> None:
    engine = engine_for(db_path)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        for paper in papers:
            session.add(paper)
        session.commit()


def _settings(tmp_path: Path) -> Settings:
    settings = Settings()
    settings.export.default_limit = 20
    settings.export.filename_max_len = 80
    settings.app.data_dir = str(tmp_path / "data")
    return settings


# ── ① 文件名清洗 ────────────────────────────────────────────────────────
def test_sanitize_illegal_chars() -> None:
    """★ 冒号 / 斜杠 / 问号 / 尖括号 / 竖线 都必须清洗（标题几乎必然含冒号）。"""
    cleaned = sanitize_title("What is 2*3? A <study> of /slash/ and |pipe|: yes")
    for bad in '\\/:*?"<>|':
        assert bad not in cleaned
    assert cleaned.startswith("What-is-2-3")


def test_sanitize_merges_dashes_and_truncates() -> None:
    assert "--" not in sanitize_title("A -- B")
    assert len(sanitize_title("A" * 200, max_len=40)) == 40
    assert sanitize_title("") == "untitled"


def test_note_name_has_date_prefix_and_short_hash() -> None:
    paper = make_paper()
    name = note_name(paper)
    assert name.startswith("2026-10-20__")
    assert len(name.rsplit("-", 1)[-1]) == 8  # ★ 重名兜底：8 位短 hash


def test_note_name_differs_for_same_title_different_ids() -> None:
    """同标题不同 id 不得撞文件名（hash 兜底）。"""
    a = note_name(make_paper("arXiv:2610.00001", "Same Title"))
    b = note_name(make_paper("arXiv:2610.00002", "Same Title"))
    assert a != b


# ── ② 标记块 ────────────────────────────────────────────────────────────
def test_marker_comments_have_no_timestamp() -> None:
    """★ 标记块注释里不能有时间戳（否则每次导出都变，与幂等冲突）。"""
    assert "BEGIN GENERATED" in BEGIN_MARKER
    assert not any(ch.isdigit() for ch in BEGIN_MARKER.replace("GENERATED", ""))
    assert END_MARKER == "<!-- END GENERATED -->"


def test_render_creates_marker_blocks_and_user_section(tmp_path: Path) -> None:
    exporter = ObsidianExporter()
    paper = make_paper()
    context = ExportContext(vault_dir=tmp_path, llm_mode="off")
    text = exporter.render(paper, context)
    assert BEGIN_MARKER in text
    assert END_MARKER in text
    assert "## 我的笔记" in text
    assert text.index(BEGIN_MARKER) < text.index(END_MARKER)


def test_user_section_is_never_overwritten(tmp_path: Path) -> None:
    """★ 用户区写入内容 → 再次导出 → 原样保留（灾难性数据丢失防线）。"""
    exporter = ObsidianExporter()
    paper = make_paper()
    context = ExportContext(vault_dir=tmp_path, llm_mode="off")
    path = exporter.note_path(paper, context)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        exporter.render(paper, context).rstrip("\n") + "\n\n我的私人笔记，不许覆盖。\n",
        encoding="utf-8",
    )
    exporter.render(paper, context)  # 再渲染一次
    text = path.read_text(encoding="utf-8")
    # render 只返回文本，需要写回才生效：这里直接比较渲染结果
    again = exporter.render(paper, context)
    assert "我的私人笔记，不许覆盖。" in again
    assert "我的私人笔记，不许覆盖。" in text


# ── ③ frontmatter 读-合并-写回 ──────────────────────────────────────────
def test_frontmatter_roundtrip() -> None:
    from ai_scholar.export.obsidian import dump_frontmatter

    fields = {"title": 'A "quoted" title', "published": "2026-10-18", "n": 1, "none": None}
    text = dump_frontmatter(fields)
    parsed, rest = parse_frontmatter(text + "\nbody")
    assert parsed["title"] == 'A "quoted" title'
    assert parsed["published"] == "2026-10-18"
    assert parsed["none"] is None
    assert rest == "body"


def test_status_is_preserved_on_reexport(tmp_path: Path) -> None:
    """★ 手改 `status: 已读` → 再次导出 → 仍是 `已读`。"""
    exporter = ObsidianExporter()
    paper = make_paper()
    context = ExportContext(vault_dir=tmp_path, llm_mode="off")
    path = exporter.note_path(paper, context)
    path.parent.mkdir(parents=True, exist_ok=True)
    original = exporter.render(paper, context).replace('status: "待读"', 'status: "已读"')
    path.write_text(original, encoding="utf-8")

    exporter.write(
        __import__("ai_scholar.export.base", fromlist=["ExportDoc"]).ExportDoc(
            path=path, content=exporter.render(paper, context)
        )
    )
    fields, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert fields["status"] == "已读"


def test_citations_not_updated_on_reexport(tmp_path: Path) -> None:
    """★ `citations` 是观测值：已有值保持不变（每次更新会产生海量 diff）。"""
    exporter = ObsidianExporter()
    paper = make_paper(citations=3)
    context = ExportContext(vault_dir=tmp_path, llm_mode="off")
    path = exporter.note_path(paper, context)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(exporter.render(paper, context), encoding="utf-8")

    paper.citation_count = 999  # 源侧涨了引用
    new_text = exporter.render(paper, context)
    fields, _ = parse_frontmatter(new_text)
    assert fields["citations"] == 3  # ★ 未被更新


def test_user_custom_fields_preserved(tmp_path: Path) -> None:
    exporter = ObsidianExporter()
    paper = make_paper()
    context = ExportContext(vault_dir=tmp_path, llm_mode="off")
    path = exporter.note_path(paper, context)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        exporter.render(paper, context).replace("---\n", "---\nmy_field: hello\n", 1),
        encoding="utf-8",
    )
    fields, _ = parse_frontmatter(exporter.render(paper, context))
    assert fields["my_field"] == "hello"


# ── ④ 双轨渲染 ──────────────────────────────────────────────────────────
def test_link_rendering_dual_track() -> None:
    """★ 有笔记 → `[[双链]]`；无笔记 → 文本 + 外链（**不建空笔记**）。"""
    ref = LinkRef(
        paper_id="arXiv:2610.00009",
        title="SWE-bench",
        url="https://arxiv.org/abs/2310.06770",
        evidence="相似性基准",
    )
    with_note = render_link_line(ref, {"arXiv:2610.00009": "2026-09-01__SWE-bench"})
    assert with_note.startswith("- [[2026-09-01__SWE-bench]]")
    assert "— 相似性基准" in with_note

    without_note = render_link_line(ref, {})
    assert "[[" not in without_note
    assert "SWE-bench" in without_note
    assert "https://arxiv.org/abs/2310.06770" in without_note


def test_link_render_does_not_create_empty_notes(tmp_path: Path) -> None:
    """★ 无笔记的目标**不得**产生空文件（§5.6：多数论文不建笔记）。"""
    exporter = ObsidianExporter()
    paper = make_paper()
    context = ExportContext(
        vault_dir=tmp_path,
        llm_mode="off",
        links=[LinkRef("arXiv:2610.99999", "Target Without Note", "https://x.test")],
    )
    text = exporter.render(paper, context)
    assert "Target Without Note" in text
    assert "https://x.test" in text
    # 只渲染了当前论文，没有为 link 目标建文件
    assert list((tmp_path / "10-文献").rglob("*.md")) == []


# ── ⑤ 原子写入 ──────────────────────────────────────────────────────────
def test_atomic_write_leaves_no_tmp(tmp_path: Path) -> None:
    from ai_scholar.export.base import ExportDoc

    exporter = ObsidianExporter()
    path = tmp_path / "a" / "b.md"
    exporter.write(ExportDoc(path=path, content="hello"))
    assert path.read_text(encoding="utf-8") == "hello"
    assert not list(tmp_path.rglob("*.tmp"))


# ── ⑥ 端到端：幂等 + 首页 + 报告 + off 档 ───────────────────────────────
@respx.mock
def test_export_is_idempotent(tmp_path: Path) -> None:
    """★ 头号验收项：连续两次 export，所有文件**字节级相同**。"""
    db_path = tmp_path / "data" / "t.db"
    _seed_db(db_path, [make_paper("arXiv:1", "Paper One: A"), make_paper("arXiv:2", "Paper Two")])
    settings = _settings(tmp_path)
    vault = tmp_path / "vault"
    network = respx.route().mock(return_value=httpx.Response(200))

    first = run_export(settings=settings, db_path=db_path, out=vault)
    snapshot = {p.relative_to(vault): p.read_bytes() for p in vault.rglob("*") if p.is_file()}
    second = run_export(settings=settings, db_path=db_path, out=vault)
    after = {p.relative_to(vault): p.read_bytes() for p in vault.rglob("*") if p.is_file()}

    assert first.exported == second.exported == 2
    assert snapshot == after, "★ 连续两次导出产生了 diff"
    assert network.call_count == 0  # ★ 导出零网络调用


def test_export_generates_index_and_report(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "t.db"
    _seed_db(db_path, [make_paper()])
    vault = tmp_path / "vault"
    report = run_export(settings=_settings(tmp_path), db_path=db_path, out=vault)

    index = vault / "00-首页.md"
    report_md = vault / "90-元数据" / "生成报告.md"
    assert index.exists() and report_md.exists()
    assert BEGIN_MARKER in index.read_text(encoding="utf-8")

    text = report_md.read_text(encoding="utf-8")
    assert "导出日期：" in text
    assert "运行档位：off" in text
    assert "LLM 用量：0 行" in text
    assert "降级状态" in text
    assert report.llm_usage_rows == 0


def test_report_exposes_degraded_state(tmp_path: Path) -> None:
    """★ 降级状态必须显式暴露（静默失败最危险）。"""
    db_path = tmp_path / "data" / "t.db"
    _seed_db(db_path, [make_paper()])
    engine = engine_for(db_path)
    with Session(engine) as session:
        session.add(
            Run(
                run_id="r1",
                started_at="2026-10-20T00:00:00+00:00",
                mode="off",
                degraded_json=json.dumps({"hf_daily": "RuntimeError: ConnectTimeout"}),
            )
        )
        session.commit()
    vault = tmp_path / "vault"
    report = run_export(settings=_settings(tmp_path), db_path=db_path, out=vault)
    assert "hf_daily" in report.degraded or "HF Daily Papers" in report.degraded
    assert "HF Daily Papers" in (vault / "90-元数据" / "生成报告.md").read_text(encoding="utf-8")


def test_export_limit_is_not_full_dump(tmp_path: Path) -> None:
    """★ 默认**不是全量**（§5.6）；`--limit` 生效。"""
    db_path = tmp_path / "data" / "t.db"
    _seed_db(db_path, [make_paper(f"arXiv:{i}", f"Paper {i}") for i in range(30)])
    vault = tmp_path / "vault"
    report = run_export(settings=_settings(tmp_path), db_path=db_path, out=vault, limit=5)
    assert report.exported == 5
    assert report.total_in_db == 30
    assert len(list((vault / "10-文献").rglob("*.md"))) == 5


def test_export_selection_order_published_desc(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "t.db"
    _seed_db(
        db_path,
        [
            make_paper("arXiv:old", "Old", published="2026-01-01T00:00:00Z"),
            make_paper("arXiv:new", "New", published="2026-10-01T00:00:00Z"),
        ],
    )
    vault = tmp_path / "vault"
    report = run_export(settings=_settings(tmp_path), db_path=db_path, out=vault, limit=1)
    assert report.exported == 1
    titles = [p.read_text(encoding="utf-8") for p in (vault / "10-文献").rglob("*.md")]
    assert any('title: "New"' in t for t in titles)


def test_export_since_filter(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "t.db"
    _seed_db(
        db_path,
        [
            make_paper("arXiv:1", "Fresh", first_seen="2026-10-20"),
            make_paper("arXiv:2", "Stale", first_seen="2026-01-01"),
        ],
    )
    vault = tmp_path / "vault"
    report = run_export(
        settings=_settings(tmp_path), db_path=db_path, out=vault, since=date(2026, 10, 1)
    )
    assert report.exported == 1


def test_watchlist_hit_starred_but_does_not_filter(tmp_path: Path) -> None:
    """★ watchlist 只打★，不决定是否导出。"""
    db_path = tmp_path / "data" / "t.db"
    _seed_db(
        db_path,
        [
            make_paper("arXiv:1", "A multimodal paper about embodied agents"),
            make_paper("arXiv:2", "A paper about something else entirely"),
        ],
    )
    vault = tmp_path / "vault"
    report = run_export(settings=_settings(tmp_path), db_path=db_path, out=vault)
    assert report.exported == 2  # ★ 未命中也要导出
    texts = [p.read_text(encoding="utf-8") for p in (vault / "10-文献").rglob("*.md")]
    assert any("★ multimodal" in t for t in texts)
    assert sum("★ multimodal" in t for t in texts) == 1


def test_exporter_abstract_exists() -> None:
    """★ C1.3：必须留 `Exporter` 抽象（即使只有一个实现）。"""
    from ai_scholar.export.base import Exporter

    assert getattr(Exporter, "__abstractmethods__", None) == frozenset({"render"})
    assert issubclass(ObsidianExporter, Exporter)


def test_no_llm_usage_after_export(tmp_path: Path) -> None:
    """★ 本阶段零 LLM 调用：`llm_usage` 0 行。"""
    from ai_scholar.store.models import LLMUsage

    db_path = tmp_path / "data" / "t.db"
    _seed_db(db_path, [make_paper()])
    run_export(settings=_settings(tmp_path), db_path=db_path, out=tmp_path / "vault")
    engine = engine_for(db_path)
    with Session(engine) as session:
        assert list(session.exec(select(LLMUsage)).all()) == []
