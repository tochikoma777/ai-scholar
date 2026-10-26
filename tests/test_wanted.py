"""L7-6：想读队列（`data/wanted.jsonl`）—— 报告/站点点读 ⇒ `selected=1`。

★ 为什么是 JSONL 而不是"站点直接置位"：静态站点无后端、public 仓库不能回写 db。
  唯一可行路径 = 把意愿写进 **public 真源**，本地消费（与记分牌同构）。
本文件守住三件事：① 幂等追加 ② 消费后真的置 `selected=1` ③ 坏行/库外 id 不静默吞。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from sqlmodel import Session
from typer.testing import CliRunner

from ai_scholar.cli import app
from ai_scholar.config import Settings
from ai_scholar.pipeline.export import consume_wanted, run_export
from ai_scholar.store.db import engine_for
from ai_scholar.store.models import Paper
from ai_scholar.store.wanted import (
    WantedEntry,
    append_wanted,
    load_wanted,
    load_wanted_with_errors,
    want_command,
    wanted_path,
)

DAY = date(2026, 10, 25)


def _make_paper(pid: str, *, selected: bool = False, first_seen: str = "2026-10-20") -> Paper:
    return Paper(
        id=pid,
        source="arxiv",
        primary_source="arxiv",
        first_seen_at=first_seen,
        published_at="2026-10-18T00:00:00Z",
        title=f"Paper {pid}",
        abstract="abstract",
        authors_json="[]",
        venue=None,
        url=f"https://arxiv.org/abs/{pid.split(':')[-1]}",
        arxiv_id=pid.split(":")[-1],
        doi=None,
        primary_category="cs.AI",
        categories_json="[]",
        content_hash="h",
        selected=selected,
    )


def _settings(tmp_path: Path) -> Settings:
    settings = Settings()
    settings.app.data_dir = str(tmp_path / "data")
    return settings


# ── ① 队列本身 ──────────────────────────────────────────────────────────
def test_append_is_idempotent(tmp_path: Path) -> None:
    """同一 id 重复标记 ⇒ 只有一行（否则队列会天天增长）。"""
    path = wanted_path(tmp_path)
    assert append_wanted(path, WantedEntry(paper_id="arXiv:1", created_at=DAY.isoformat()))
    assert not append_wanted(path, WantedEntry(paper_id="arXiv:1", created_at=DAY.isoformat()))
    assert len(load_wanted(path)) == 1


def test_bad_lines_are_reported_not_swallowed(tmp_path: Path) -> None:
    """★ 坏行必须被报出来（静默失败最危险）。"""
    path = wanted_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"paper_id": "arXiv:1", "created_at": "2026-10-25"}\n'
        "not-json\n"
        '{"paper_id": "arXiv:2", "created_at": "2026-10-25"}\n',
        encoding="utf-8",
    )
    entries, bad = load_wanted_with_errors(path)
    assert [e.paper_id for e in entries] == ["arXiv:1", "arXiv:2"]
    assert len(bad) == 1


def test_want_command_is_the_single_source() -> None:
    assert want_command("arXiv:1") == "uv run ai-scholar want arXiv:1"


# ── ② CLI `want` ────────────────────────────────────────────────────────
def test_cli_want_writes_queue_and_selects(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_SCHOLAR_ROOT", str(tmp_path))
    settings = _settings(tmp_path)
    engine = engine_for(settings.db_path())
    from sqlmodel import SQLModel

    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(_make_paper("arXiv:1"))
        session.commit()

    result = CliRunner().invoke(app, ["want", "arXiv:1"])
    assert result.exit_code == 0, result.output
    assert "想读" in result.output

    assert [e.paper_id for e in load_wanted(wanted_path(settings.data_dir()))] == ["arXiv:1"]
    with Session(engine) as session:
        paper = session.get(Paper, "arXiv:1")
        assert paper is not None and paper.selected is True


def test_cli_want_unknown_id_only_queues(tmp_path: Path, monkeypatch) -> None:
    """库内没有的 id：入队但不报错（采集后由 export 消费生效）。"""
    monkeypatch.setenv("AI_SCHOLAR_ROOT", str(tmp_path))
    settings = _settings(tmp_path)
    settings.db_path().parent.mkdir(parents=True, exist_ok=True)

    result = CliRunner().invoke(app, ["want", "arXiv:404"])
    assert result.exit_code == 0, result.output
    assert "已入队" in result.output
    assert load_wanted(wanted_path(settings.data_dir()))


# ── ③ export 消费 ───────────────────────────────────────────────────────
def test_export_consumes_queue(tmp_path: Path) -> None:
    """★ 消费后 `selected=1`，于是 `export`（默认只导 selected）能把它导出。"""
    settings = _settings(tmp_path)
    db_path = settings.db_path()
    engine = engine_for(db_path)
    from sqlmodel import SQLModel

    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(_make_paper("arXiv:1"))  # 未收藏
        session.commit()

    path = wanted_path(settings.data_dir())
    append_wanted(path, WantedEntry(paper_id="arXiv:1", created_at=DAY.isoformat()))

    vault = tmp_path / "vault"
    report = run_export(settings=settings, db_path=db_path, out=vault)
    assert report.wanted_applied == 1
    assert report.wanted_total == 1
    assert report.exported == 1, "点读的论文应被导出成笔记"
    assert list((vault / "10-文献").rglob("*.md"))


def test_export_reports_ids_missing_from_db(tmp_path: Path) -> None:
    """★ 队列里库内没有的 id ⇒ 进 degraded（不能装作没发生）。"""
    settings = _settings(tmp_path)
    db_path = settings.db_path()
    engine = engine_for(db_path)
    from sqlmodel import SQLModel

    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(_make_paper("arXiv:1"))
        session.commit()

    append_wanted(
        wanted_path(settings.data_dir()),
        WantedEntry(paper_id="arXiv:404", created_at=DAY.isoformat()),
    )
    report = run_export(settings=settings, db_path=db_path, out=tmp_path / "vault")
    assert report.wanted_applied == 0
    assert "404" in report.degraded.get("wanted", "")


def test_consume_is_idempotent(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    db_path = settings.db_path()
    engine = engine_for(db_path)
    from sqlmodel import SQLModel

    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(_make_paper("arXiv:1"))
        session.commit()
    append_wanted(
        wanted_path(settings.data_dir()),
        WantedEntry(paper_id="arXiv:1", created_at=DAY.isoformat()),
    )

    engine = engine_for(db_path)
    with Session(engine) as session:
        first = consume_wanted(session, settings)
        session.commit()
    with Session(engine) as session:
        second = consume_wanted(session, settings)
    assert first[0] == 1
    assert second[0] == 0, "已 selected 的不该重复计数"


# ── ④ 两个入口都给出可复制命令 ──────────────────────────────────────────
def test_report_template_has_want_command(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    db_path = settings.db_path()
    engine = engine_for(db_path)
    from sqlmodel import SQLModel

    from ai_scholar.models import WatchlistConfig, WatchlistGroup
    from ai_scholar.pipeline.report import build_daily_report, render_markdown

    watchlist = WatchlistConfig(
        default_threshold=0.9,
        groups=[WatchlistGroup(name="g", weight=1.0, keywords=["paper"])],
    )

    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(_make_paper("arXiv:1", first_seen=DAY.isoformat()))
        session.commit()
        report = build_daily_report(session, today=DAY, watchlist=watchlist)
        text = render_markdown(report)
    assert "uv run ai-scholar want" in text
    assert "想读" in text


def test_site_html_has_copy_button(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = engine_for(settings.db_path())
    from sqlmodel import SQLModel

    from ai_scholar.pipeline.site import run_site

    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(_make_paper("arXiv:1", selected=True))
        session.commit()

    out = tmp_path / "site"
    run_site(out=out, settings=settings, today=DAY)
    html = (out / "index.html").read_text(encoding="utf-8")
    assert 'class="want"' in html and "uv run ai-scholar want arXiv:1" in html
    # ★ 零外部请求：按钮只操作剪贴板，不许出现 fetch / XHR
    assert "fetch(" not in html and "XMLHttpRequest" not in html
