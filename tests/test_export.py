"""L0-C：Obsidian 导出器。

★ 本阶段**头号验收项**是幂等：连续两次 `export` 必须**无 diff**。
其余硬约束：标记块不覆盖用户区、frontmatter 读-合并-写回、文件名清洗、
双轨渲染（无笔记不建空文件）、原子写入、`citations` 不每次更新。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from sqlmodel import Session, SQLModel, select
from typer.testing import CliRunner

from ai_scholar.cli import app
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
from ai_scholar.store.models import LINK_TYPE_SIMILARITY, Paper, Run
from ai_scholar.utils.errors import UnsafeOutputPath

runner = CliRunner()


def make_paper(
    pid: str = "arXiv:2610.00001",
    title: str = "A Study of Things: With a Colon",
    *,
    first_seen: str = "2026-10-20",
    published: str = "2026-10-18T12:00:00Z",
    hf_score: int | None = 7,
    citations: int | None = 3,
    category: str = "cs.AI",
    selected: bool = True,
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
        selected=selected,  # ★ 本文件的用例默认把论文视为"已收藏"，专测导出机制
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


def test_refs_count_not_updated_on_reexport(tmp_path: Path) -> None:
    """★ `refs_count` 同为观测值（L1-B signals 会刷新它）⇒ 不得每次导出都改笔记。"""
    exporter = ObsidianExporter()
    paper = make_paper()
    paper.refs_count = 12
    context = ExportContext(vault_dir=tmp_path, llm_mode="off")
    path = exporter.note_path(paper, context)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(exporter.render(paper, context), encoding="utf-8")

    paper.refs_count = 99  # signals 采样刷新了它
    fields, _ = parse_frontmatter(exporter.render(paper, context))
    assert fields["refs_count"] == 12  # ★ 未被更新（否则全库笔记反复产生 diff）


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
        link_type=LINK_TYPE_SIMILARITY,
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
        out_links=[LinkRef("arXiv:2610.99999", "Target Without Note", "https://x.test")],
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
    """★ 头号验收项：连续两次 export，幂等范围内文件**字节级相同**。

    ★ v1.4：`90-元数据/生成报告.md` 是运行日志（秒级时间），**允许 diff**，不在比对范围。
    """
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
    report_rel = Path("90-元数据") / "生成报告.md"
    assert report_rel in snapshot and report_rel in after  # 报告存在，但允许 diff
    snapshot.pop(report_rel)
    after.pop(report_rel)
    assert snapshot == after, "★ 连续两次导出在幂等范围内产生了 diff"
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
    assert "导出时间：" in text
    assert "运行档位：off" in text
    assert "LLM 用量：0 行" in text
    assert "降级状态" in text
    assert report.llm_usage_rows == 0


def test_report_time_is_second_level_and_changes_every_run(tmp_path: Path) -> None:
    """★ 报告是运行日志：秒级时间，每次导出都应更新（v1.4 修正 L0-C 的错误取舍）。"""
    db_path = tmp_path / "data" / "t.db"
    _seed_db(db_path, [make_paper()])
    vault = tmp_path / "vault"
    settings = _settings(tmp_path)
    report_md = vault / "90-元数据" / "生成报告.md"

    first_at = datetime(2026, 10, 23, 13, 5, 7, tzinfo=UTC)
    run_export(settings=settings, db_path=db_path, out=vault, exported_at=first_at)
    first = report_md.read_text(encoding="utf-8")
    assert "导出时间：2026-10-23 21:05:07" in first  # Asia/Shanghai

    run_export(
        settings=settings,
        db_path=db_path,
        out=vault,
        exported_at=first_at + timedelta(seconds=1),
    )
    second = report_md.read_text(encoding="utf-8")
    assert "导出时间：2026-10-23 21:05:08" in second
    assert first != second, "★ 生成报告必须每次导出都更新（运行日志）"


def test_export_check_lists_changes_without_writing(tmp_path: Path) -> None:
    """★ `--check`：首次列出全部新建且不落盘；二次为空（幂等可自动验证）。"""
    db_path = tmp_path / "data" / "t.db"
    _seed_db(db_path, [make_paper()])
    vault = tmp_path / "vault"
    settings = _settings(tmp_path)

    first = run_export(settings=settings, db_path=db_path, out=vault, check=True)
    assert first.changed, "首次导出应列出全部新建"
    assert not vault.exists(), "★ --check 不得写盘（连目录都不建）"
    assert all(p.name != "生成报告.md" for p in first.changed)  # 报告是运行日志，不参与

    run_export(settings=settings, db_path=db_path, out=vault)
    second = run_export(settings=settings, db_path=db_path, out=vault, check=True)
    assert second.changed == []
    assert second.check is True


def test_export_rejects_target_inside_repo(tmp_path: Path, repo_root: Path) -> None:
    """★ 双仓库防护 3：目标落在代码仓库内（含 example_vault）必须拒绝。"""
    settings = _settings(tmp_path)
    with pytest.raises(UnsafeOutputPath):
        run_export(settings=settings, db_path=tmp_path / "t.db", out=repo_root / "example_vault")
    with pytest.raises(UnsafeOutputPath):
        run_export(settings=settings, db_path=tmp_path / "t.db", out=repo_root / "vault")


def test_export_check_cli_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """★ `export --check`：有变更退出 1，无变更退出 0（CI 可断言）。"""
    root = tmp_path / "repo"
    monkeypatch.setenv("AI_SCHOLAR_ROOT", str(root))
    monkeypatch.setenv("AI_SCHOLAR_EXPORT__OUT_DIR", str(tmp_path / "vault"))
    _seed_db(root / "data" / "ai_scholar.db", [make_paper()])
    runner = CliRunner()

    first = runner.invoke(app, ["export", "--check"], env={"COLUMNS": "200"})
    assert first.exit_code == 1
    assert "将变更" in first.output
    assert not (tmp_path / "vault").exists()

    assert runner.invoke(app, ["export"], env={"COLUMNS": "200"}).exit_code == 0
    second = runner.invoke(app, ["export", "--check"], env={"COLUMNS": "200"})
    assert second.exit_code == 0
    assert "无变更" in second.output


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


def test_content_rev_at_not_bumped_across_days_without_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 跨天重复导出、内容没变 ⇒ `content_rev_at` 不得变（否则 --check 每天误报）。

    标记块两侧的换行是框架，不属于内容；比较时必须剥掉（L1-A 跨天后暴露的坑）。
    """
    from ai_scholar.export.base import ExportDoc

    exporter = ObsidianExporter()
    paper = make_paper()
    context = ExportContext(vault_dir=tmp_path, llm_mode="off")
    path = exporter.note_path(paper, context)

    monkeypatch.setattr("ai_scholar.export.obsidian.today_local", lambda: date(2026, 10, 23))
    exporter.write(ExportDoc(path=path, content=exporter.render(paper, context)))
    first = path.read_text(encoding="utf-8")
    assert "content_rev_at: 2026-10-23" in first

    monkeypatch.setattr("ai_scholar.export.obsidian.today_local", lambda: date(2026, 10, 24))
    exporter.write(ExportDoc(path=path, content=exporter.render(paper, context)))
    second = path.read_text(encoding="utf-8")

    assert second == first  # ★ 内容没变，整文件（含 content_rev_at）都不该变


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


# ── ⑦ 选集策略（§5.6 v1.7）──────────────────────────────────────────────
def test_export_default_only_includes_selected(tmp_path: Path) -> None:
    """★ 默认只导 selected=1（否则每天 20 篇 = 7,300 篇/年）。"""
    db_path = tmp_path / "data" / "t.db"
    _seed_db(
        db_path,
        [
            make_paper("arXiv:1", "Selected Paper", selected=True),
            make_paper("arXiv:2", "Unselected Paper", selected=False),
        ],
    )
    vault = tmp_path / "vault"
    report = run_export(settings=_settings(tmp_path), db_path=db_path, out=vault)
    assert report.exported == 1
    names = [p.name for p in (vault / "10-文献").rglob("*.md")]
    assert any("Selected-Paper" in n for n in names)
    assert not any("Unselected-Paper" in n for n in names)


def test_export_all_includes_unselected(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "t.db"
    _seed_db(
        db_path,
        [
            make_paper("arXiv:1", "Selected Paper", selected=True),
            make_paper("arXiv:2", "Unselected Paper", selected=False),
        ],
    )
    vault = tmp_path / "vault"
    report = run_export(settings=_settings(tmp_path), db_path=db_path, out=vault, all_papers=True)
    assert report.exported == 2


def test_index_describes_vault_not_current_selection(tmp_path: Path) -> None:
    """★ 首页只描述 vault 状态：换选集（默认 / --ids）不得改变首页（否则幂等失效）。"""
    db_path = tmp_path / "data" / "t.db"
    _seed_db(
        db_path,
        [
            make_paper("arXiv:1", "Paper A"),
            make_paper("arXiv:2", "Paper B"),
        ],
    )
    vault = tmp_path / "vault"
    settings = _settings(tmp_path)
    run_export(settings=settings, db_path=db_path, out=vault)
    first = (vault / "00-首页.md").read_text(encoding="utf-8")
    assert "有笔记论文数：2" in first
    assert "本次导出" not in first

    # 只导 A（--ids）⇒ 首页仍应统计到 2 篇笔记
    run_export(settings=settings, db_path=db_path, out=vault, ids=["arXiv:1"])
    second = (vault / "00-首页.md").read_text(encoding="utf-8")
    assert first == second


def test_export_empty_selection_is_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 无 selected 时给明确提示，不静默导出 0 篇、不写空首页。"""
    root = tmp_path / "repo"
    _seed_db(root / "data" / "ai_scholar.db", [make_paper(selected=False)])
    monkeypatch.setenv("AI_SCHOLAR_ROOT", str(root))
    monkeypatch.setenv("AI_SCHOLAR_EXPORT__OUT_DIR", str(tmp_path / "vault"))

    result = runner.invoke(app, ["export"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "尚无选中论文" in result.output
    assert not (tmp_path / "vault" / "00-首页.md").exists()

    checked = runner.invoke(app, ["export", "--check"], env={"COLUMNS": "200"})
    assert checked.exit_code == 0
    assert "尚无选中论文" in checked.output


def test_mark_cli_toggles_selected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "repo"
    db_path = root / "data" / "ai_scholar.db"
    _seed_db(db_path, [make_paper("arXiv:1", "A Paper", selected=False)])
    monkeypatch.setenv("AI_SCHOLAR_ROOT", str(root))

    marked = runner.invoke(app, ["mark", "arXiv:1"], env={"COLUMNS": "200"})
    assert marked.exit_code == 0, marked.output
    with Session(engine_for(db_path)) as session:
        assert session.get(Paper, "arXiv:1").selected is True

    unmarked = runner.invoke(app, ["mark", "arXiv:1", "--unmark"], env={"COLUMNS": "200"})
    assert unmarked.exit_code == 0
    with Session(engine_for(db_path)) as session:
        assert session.get(Paper, "arXiv:1").selected is False

    missing = runner.invoke(app, ["mark", "arXiv:404"], env={"COLUMNS": "200"})
    assert missing.exit_code == 2


def test_check_all_covers_notes_outside_selection(tmp_path: Path) -> None:
    """★ `--check --all` 校验全量笔记（补 L1-0 只校验本次选集的局限）。"""
    db_path = tmp_path / "data" / "t.db"
    _seed_db(
        db_path,
        [
            make_paper("arXiv:1", "Paper A"),
            make_paper("arXiv:2", "Paper B"),
        ],
    )
    vault = tmp_path / "vault"
    run_export(settings=_settings(tmp_path), db_path=db_path, out=vault)

    # B 不再选中，且磁盘上的笔记被改动 ⇒ 默认 --check 看不到，--all 必须报出来
    with Session(engine_for(db_path)) as session:
        session.get(Paper, "arXiv:2").selected = False
        session.commit()
    note_b = next((vault / "10-文献").rglob("*Paper-B*.md"))
    original = note_b.read_text(encoding="utf-8")
    # ★ 改**生成区**（用户区会被合法保留，改那里不算 diff）
    patched = original.replace("## 方法标签", "## 方法标签（被篡改）", 1)
    assert patched != original
    note_b.write_text(patched, encoding="utf-8")

    default = run_export(settings=_settings(tmp_path), db_path=db_path, out=vault, check=True)
    assert note_b not in default.changed  # ★ 默认范围看不到未选中的笔记

    full = run_export(
        settings=_settings(tmp_path),
        db_path=db_path,
        out=vault,
        check=True,
        all_papers=True,
    )
    assert note_b in full.changed  # ★ --all 覆盖全量笔记


def test_enhance_fields_rendered_or_dash(tmp_path: Path) -> None:
    """★ L3a：有增强字段就渲染，没有就显式 `—`。"""
    paper = make_paper()
    paper.one_line = "一句话内容"
    paper.why = "为什么重要内容"
    paper.method_tags_json = json.dumps(["agent", "benchmark"], ensure_ascii=False)
    text = ObsidianExporter().render(paper, ExportContext(vault_dir=tmp_path, llm_mode="off"))
    assert "一句话内容" in text
    assert "为什么重要内容" in text
    assert "`agent`" in text
    assert "`benchmark`" in text

    plain = make_paper()
    text2 = ObsidianExporter().render(plain, ExportContext(vault_dir=tmp_path, llm_mode="off"))
    assert "## 一句话\n—" in text2
    assert "## 为什么重要\n—" in text2
    assert "## 方法标签\n—" in text2


def test_link_rendering_shows_interpretation() -> None:
    """★ L3a：强相似边的 interpretation 追加在 evidence 之后。"""
    ref = LinkRef(
        paper_id="arXiv:2610.00009",
        title="SWE-bench",
        url="https://arxiv.org/abs/2310.06770",
        link_type=LINK_TYPE_SIMILARITY,
        evidence="相似 0.42",
        interpretation="同一方向的演进",
    )
    line = render_link_line(ref, {})
    assert "相似 0.42" in line
    assert "同一方向的演进" in line


def test_frontmatter_tier_rendered(tmp_path: Path) -> None:
    """★ L3b-1：定级 tier 进 frontmatter；未定级为 null。"""
    paper = make_paper()
    paper.tier = 1
    text = ObsidianExporter().render(paper, ExportContext(vault_dir=tmp_path, llm_mode="fast"))
    assert "tier: 1" in text

    plain = make_paper()
    text2 = ObsidianExporter().render(plain, ExportContext(vault_dir=tmp_path, llm_mode="off"))
    assert "tier: null" in text2


def test_deep_analysis_rendered(tmp_path: Path) -> None:
    """★ L3b-2：深度分析 Markdown 追加到生成区。"""
    paper = make_paper()
    paper.deep_analysis_md = "## TL;DR\n坐标：演进\n## 六段\n### 总体介绍\n内容"
    text = ObsidianExporter().render(paper, ExportContext(vault_dir=tmp_path, llm_mode="strong"))
    assert "## TL;DR" in text
    assert "### 总体介绍" in text
    assert "内容" in text


# ── L2-C Daily Note（§6.6）──────────────────────────────────────────────
def test_daily_note_generated(tmp_path: Path) -> None:
    """Daily Note 落 `40-日志/YYYY-MM-DD.md`：生成区在标记块内，待办在用户区。"""
    db_path = tmp_path / "data" / "t.db"
    _seed_db(db_path, [make_paper("arXiv:1", "Paper One"), make_paper("arXiv:2", "Paper Two")])
    vault = tmp_path / "vault"
    run_export(settings=_settings(tmp_path), db_path=db_path, out=vault, today=date(2026, 10, 20))

    note = vault / "40-日志" / "2026-10-20.md"
    assert note.exists()
    text = note.read_text(encoding="utf-8")
    assert "今日新增（2 篇）" in text
    assert BEGIN_MARKER in text and END_MARKER in text
    assert "## 我的笔记摘要" in text
    assert "## 待办" in text
    # ★ 用户区必须在标记块**之外**
    assert text.index(END_MARKER) < text.index("## 我的笔记摘要")


def test_daily_note_user_section_preserved(tmp_path: Path) -> None:
    """★ 用户写的摘要/待办永不覆盖。"""
    db_path = tmp_path / "data" / "t.db"
    _seed_db(db_path, [make_paper("arXiv:1", "Paper One")])
    vault = tmp_path / "vault"
    run_export(settings=_settings(tmp_path), db_path=db_path, out=vault, today=date(2026, 10, 20))

    note = vault / "40-日志" / "2026-10-20.md"
    original = note.read_text(encoding="utf-8")
    patched = original.replace("## 我的笔记摘要", "## 我的笔记摘要\n今天读了 Paper One", 1)
    note.write_text(patched, encoding="utf-8")

    run_export(settings=_settings(tmp_path), db_path=db_path, out=vault, today=date(2026, 10, 20))
    assert "今天读了 Paper One" in note.read_text(encoding="utf-8")


def test_daily_note_idempotent(tmp_path: Path) -> None:
    """连续两次导出，Daily Note 无 diff（文件内不留导出时间）。"""
    db_path = tmp_path / "data" / "t.db"
    _seed_db(db_path, [make_paper("arXiv:1", "Paper One")])
    vault = tmp_path / "vault"
    run_export(settings=_settings(tmp_path), db_path=db_path, out=vault, today=date(2026, 10, 20))
    note = vault / "40-日志" / "2026-10-20.md"
    first = note.read_text(encoding="utf-8")

    run_export(settings=_settings(tmp_path), db_path=db_path, out=vault, today=date(2026, 10, 20))
    assert note.read_text(encoding="utf-8") == first
