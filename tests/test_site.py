"""L5a：静态 HTML 单文件站点。

★ 验收重点不是"有 HTML"，而是四条硬约束：
  1. 只读 public 数据（vault 里的私有笔记不得出现）；
  2. 自包含（无 script / link / CDN / url()）；
  3. 分项分并列展示，**不合成单一总分**；
  4. 输出确定性（重跑逐字节一致，`--check` 可断言）。
另测：不可信文本转义、`javascript:` 链接被丢弃、体积上限、缺输入时显式降级。
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel
from typer.testing import CliRunner

from ai_scholar.config import AppSettings, CalibrationSettings, ExportSettings, Settings
from ai_scholar.export.base import ExportContext
from ai_scholar.export.html import MAX_BYTES, HtmlExporter, SiteData, SitePaper
from ai_scholar.models import WatchlistConfig, WatchlistGroup
from ai_scholar.pipeline.report import (
    DailyReport,
    PredictionBrief,
    ReportHit,
    write_report,
)
from ai_scholar.pipeline.site import run_site
from ai_scholar.store.db import engine_for
from ai_scholar.store.models import Paper
from ai_scholar.store.scoreboard import (
    Criterion,
    Opinion,
    Prediction,
    append_opinion,
    append_snapshot,
    opinions_path,
    scoreboard_path,
)
from ai_scholar.utils.errors import ConfigError

TODAY = date(2026, 10, 25)
runner = CliRunner()


def _settings(tmp_path: Path, *, min_sample_per_tier: int = 1) -> Settings:
    return Settings(
        app=AppSettings(data_dir=str(tmp_path / "data")),
        export=ExportSettings(site_dir=str(tmp_path / "site")),
        calibration=CalibrationSettings(min_sample_per_tier=min_sample_per_tier),
    )


def _seed_reports(
    tmp_path: Path,
    days: list[tuple[date, float]],
    *,
    anomalies: list[str] | None = None,
) -> None:
    """写若干天的报告 JSON（站点只读报告，异常字段由 report 侧算好再落盘）。"""
    for day, cost in days:
        write_report(
            DailyReport(
                date=day.isoformat(),
                cost_cny=cost,
                cost_anomalies=list(anomalies or []),
            ),
            tmp_path / "data" / "reports",
        )


def make_paper(
    pid: str,
    *,
    title: str = "A Paper About Agents",
    url: str | None = "https://arxiv.org/abs/2610.00001",
    first_seen: str = "2026-10-25",
    published: str = "2026-10-24T00:00:00Z",
    tier: int | None = 1,
    citations: int | None = 5,
    note_path: str | None = None,
) -> Paper:
    return Paper(
        id=pid,
        source="arxiv",
        primary_source="arxiv",
        first_seen_at=first_seen,
        published_at=published,
        title=title,
        abstract="abstract text",
        authors_json=json.dumps(["A. Author"]),
        url=url,
        arxiv_id=pid.split(":", 1)[-1],
        primary_category="cs.AI",
        hf_score=7,
        citation_count=citations,
        content_hash=f"hash-{pid}",
        note_path=note_path,
        tier=tier,
        one_line="一句话结论",
        why="为什么重要",
    )


def _seed_db(tmp_path: Path, papers: list[Paper]) -> None:
    engine = engine_for(tmp_path / "data" / "ai_scholar.db")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        for paper in papers:
            session.add(paper)
        session.commit()


def _seed_report(tmp_path: Path, day: str = TODAY.isoformat()) -> None:
    report = DailyReport(
        date=day,
        total_papers=6,
        new_today=2,
        cost_cny=0.2812,
        watchlist_hits=[ReportHit(id="arXiv:2610.00001", title="A Paper", score=1.0)],
        predictions_new=[
            PredictionBrief(
                id="p-new", claim="新推演：Agent 论文会增多", status="open", due_at="2026-11-10"
            )
        ],
        predictions_verified=[
            PredictionBrief(
                id="p-hit",
                claim="已验证：cs.AI 周增超过阈值",
                status="hit",
                due_at="2026-10-20",
                observed=7,
                threshold=5,
            )
        ],
        predictions_due=[
            PredictionBrief(
                id="p-due", claim="到期未验证的推演", status="open", due_at="2026-10-24"
            )
        ],
        opinions_total=2,
        degraded={"HF Daily Papers": "不可达（测试）"},
    )
    write_report(report, tmp_path / "data" / "reports")


def _seed_scoreboard(tmp_path: Path) -> None:
    path = scoreboard_path(tmp_path / "data")
    criterion = Criterion(type="arxiv_paper_count", target="cs.AI", threshold=5, window_days=7)
    for pid, status, observed in (
        ("p-hit", "hit", 7),
        ("p-miss", "miss", 2),
        ("p-insufficient", "insufficient", None),
        ("p-open", "open", None),
    ):
        append_snapshot(
            path,
            Prediction(
                id=pid,
                claim=f"claim {pid}",
                criterion=criterion,
                created_at="2026-10-01",
                due_at="2026-10-20",
                status=status,
                observed=observed,
            ),
        )
    append_opinion(
        opinions_path(tmp_path / "data"),
        Opinion(id="o-1", text="长期趋势", created_at="2026-10-01"),
    )


def _full_dataset(tmp_path: Path, papers: list[Paper] | None = None) -> Settings:
    settings = _settings(tmp_path)
    _seed_db(
        tmp_path,
        papers
        or [
            make_paper("arXiv:2610.00001", title="A Paper About Agents"),
            make_paper("arXiv:2610.00002", title="Second Paper", tier=2, citations=1),
        ],
    )
    _seed_report(tmp_path)
    _seed_scoreboard(tmp_path)
    return settings


def test_site_renders_required_sections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ai_scholar.pipeline.site.load_watchlist",
        lambda: WatchlistConfig(groups=[WatchlistGroup(name="Agents", keywords=["agents"])]),
    )
    settings = _full_dataset(tmp_path)
    report = run_site(settings=settings, today=TODAY)

    html = report.out_path.read_text(encoding="utf-8")
    for heading in ("今日概览", "今日推演", "记分板", "定级校准", "论文（分项分）", "近期概览"):
        assert heading in html
    # 今日推演三类都在（新增 / 已验证 / 到期未验证）
    assert "新推演：Agent 论文会增多" in html
    assert "（观测 7 / 阈值 5）" in html
    assert "到期未验证的推演" in html
    # 命中率分母 = hit + miss（insufficient 不进分母）
    assert "50.0%" in html
    assert "数据不足（不计入分母）" in html
    # ★ L6-1：报告自身的降级必须透出到页面（静默失败第一现场），
    #   不能只显示"输入缺失"——否则源在报错而页面照样"正常"
    assert "降级状态" in html
    assert "HF Daily Papers" in html
    assert "不可达（测试）" in html
    # L6-2：当日 LLM 成本来自报告字段
    assert "今日 LLM 成本" in html
    assert "¥0.2812" in html
    # 校准 + 论文元数据
    assert "Tier 1" in html and "Tier 2" in html
    assert "A Paper About Agents" in html
    assert "为什么重要" in html
    assert report.papers == 2
    assert report.reports == 1


def test_site_shows_subscores_without_single_total(tmp_path: Path) -> None:
    settings = _full_dataset(tmp_path)
    html = run_site(settings=settings, today=TODAY).out_path.read_text(encoding="utf-8")
    assert "热度" in html and "跨源" in html and "主题" in html
    assert "总分" not in html
    assert "不合并为单一分数" in html


def test_site_escapes_text_and_drops_javascript_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "ai_scholar.pipeline.site.load_watchlist",
        lambda: WatchlistConfig(groups=[WatchlistGroup(name="<b>组</b>", keywords=["evil"])]),
    )
    settings = _full_dataset(
        tmp_path,
        [
            make_paper(
                "arXiv:2610.00003",
                title='<script>alert("evil")</script>',
                url="javascript:alert(1)",
            )
        ],
    )
    html = run_site(settings=settings, today=TODAY).out_path.read_text(encoding="utf-8")

    assert '<script>alert("evil")</script>' not in html
    assert "&lt;script&gt;alert" in html
    assert "javascript:" not in html
    assert "&lt;b&gt;组&lt;/b&gt;" in html


def test_site_is_deterministic_and_check_is_honest(tmp_path: Path) -> None:
    settings = _full_dataset(tmp_path)
    first = run_site(settings=settings, today=TODAY)
    assert first.written and first.changed
    before = first.out_path.read_bytes()

    again = run_site(settings=settings, today=TODAY)
    assert again.out_path.read_bytes() == before

    assert run_site(settings=settings, today=TODAY, check=True).changed is False

    # 新增一篇 ⇒ 输入变了，--check 必须报变更且**不写盘**
    _seed_db(tmp_path, [make_paper("arXiv:2610.00009", title="Brand New Paper")])
    changed = run_site(settings=settings, today=TODAY, check=True)
    assert changed.changed is True
    assert changed.written is False
    assert changed.out_path.read_bytes() == before


def test_site_is_self_contained(tmp_path: Path) -> None:
    settings = _full_dataset(tmp_path)
    html = run_site(settings=settings, today=TODAY).out_path.read_text(encoding="utf-8")
    for forbidden in ("<link", "src=", "@import", "url("):
        assert forbidden not in html
    assert "<style>" in html
    # ★ L5b：允许内联 JS，但不得加载任何外部资源
    assert html.count("<script>") == 1
    assert html.count("</script>") == 2  # 一个 JSON 数据块 + 一个脚本块
    for script in re.findall(r"<script>(.*?)</script>", html, re.S):
        for token in ("fetch(", "XMLHttpRequest", "http://", "https://", "import("):
            assert token not in script


def test_site_embeds_filter_payload_and_card_attributes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "ai_scholar.pipeline.site.load_watchlist",
        lambda: WatchlistConfig(groups=[WatchlistGroup(name="Agents", keywords=["agents"])]),
    )
    settings = _full_dataset(tmp_path)
    html = run_site(settings=settings, today=TODAY).out_path.read_text(encoding="utf-8")

    for control in ('id="q"', 'id="group"', 'id="tier"', 'id="date"', 'id="sort"'):
        assert control in html

    payload = re.search(
        r'<script type="application/json" id="site-index">(.*?)</script>', html, re.S
    )
    assert payload is not None
    manifest = json.loads(payload.group(1))
    assert manifest["version"] == 1
    assert manifest["total"] == 2
    assert manifest["groups"] == ["Agents"]
    assert manifest["tiers"] == [1, 2]

    card = re.search(r'<article class="paper" data-id="arXiv:2610\.00001"[^>]*>', html)
    assert card is not None
    attrs = card.group(0)
    assert 'data-tier="1"' in attrs
    assert 'data-date="2026-10-25"' in attrs
    assert 'data-groups="[&quot;Agents&quot;]"' in attrs
    assert "a paper about agents" in attrs.lower()
    for key in ("data-heat=", "data-cross=", "data-topic="):
        assert key in attrs


def test_site_manifest_neutralizes_script_close() -> None:
    hostile = "</script><script>alert(1)</script>"
    page = HtmlExporter().render_site(
        SiteData(
            date="2026-10-25",
            papers=[SitePaper(paper_id="p-1", title="t", groups=(hostile,))],
        )
    )
    assert hostile not in page
    payload = re.search(
        r'<script type="application/json" id="site-index">(.*?)</script>', page, re.S
    )
    assert payload is not None
    assert json.loads(payload.group(1))["groups"] == [hostile]


def test_site_cards_are_readable_without_js(tmp_path: Path) -> None:
    settings = _full_dataset(tmp_path)
    html = run_site(settings=settings, today=TODAY).out_path.read_text(encoding="utf-8")
    assert 'id="paper-list"' in html
    assert html.count('<article class="paper"') == 2
    assert "<noscript>" in html


def test_site_never_reads_vault(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "secret.md").write_text("SECRET-VAULT-NOTE", encoding="utf-8")
    settings.export.out_dir = str(vault)
    _seed_db(tmp_path, [make_paper("arXiv:2610.00001", note_path="SECRET-VAULT-NOTE.md")])
    _seed_report(tmp_path)
    _seed_scoreboard(tmp_path)

    html = run_site(settings=settings, today=TODAY).out_path.read_text(encoding="utf-8")
    assert "SECRET-VAULT-NOTE" not in html
    assert "我的笔记" not in html


def test_site_empty_inputs_declare_degraded_state(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    report = run_site(settings=settings, today=TODAY)
    html = report.out_path.read_text(encoding="utf-8")
    assert "无 data/reports/*.json" in html
    assert "数据库不存在" in html
    assert "（库内暂无论文）" in html
    assert "降级状态" in html
    # ★ public 产物不得带本机绝对路径（降级文案也不能）
    assert str(tmp_path) not in html
    # off 档显示 ¥0，不是「—」
    assert "¥0.0000" in html


def test_site_calibration_insufficient_does_not_conclude(tmp_path: Path) -> None:
    settings = _settings(tmp_path, min_sample_per_tier=20)
    _seed_db(tmp_path, [make_paper("arXiv:2610.00001", tier=1, citations=5)])
    html = run_site(settings=settings, today=TODAY).out_path.read_text(encoding="utf-8")
    assert "样本不足" in html
    assert "不下结论" in html


def test_site_size_guard_fails_loudly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _full_dataset(tmp_path)
    monkeypatch.setattr("ai_scholar.export.html.MAX_BYTES", 100)
    with pytest.raises(ConfigError, match="超过上限"):
        run_site(settings=settings, today=TODAY)
    assert MAX_BYTES > 100  # 防止 monkeypatch 泄漏成实际常量


def test_site_cli_check_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _full_dataset(tmp_path)
    monkeypatch.setattr("ai_scholar.pipeline.site.load_settings", lambda: settings)

    out_dir = tmp_path / "cli-site"
    assert runner.invoke(runner_app(), ["site", "--out", str(out_dir)]).exit_code == 0
    assert (out_dir / "index.html").exists()

    again = runner.invoke(runner_app(), ["site", "--out", str(out_dir), "--check"])
    assert again.exit_code == 0

    _seed_db(tmp_path, [make_paper("arXiv:2610.00010", title="Another New Paper")])
    changed = runner.invoke(runner_app(), ["site", "--out", str(out_dir), "--check"])
    assert changed.exit_code == 1


def test_site_cli_renders_without_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr("ai_scholar.pipeline.site.load_settings", lambda: settings)
    out_dir = tmp_path / "empty-site"
    result = runner.invoke(runner_app(), ["site", "--out", str(out_dir)])
    assert result.exit_code == 0
    assert (out_dir / "index.html").exists()


def runner_app() -> object:
    """延迟导入：避免测试收集时就绑定 typer app（导入顺序无关）。"""
    from ai_scholar.cli import app

    return app


def test_site_path_is_index_html(tmp_path: Path) -> None:
    assert HtmlExporter().site_path(tmp_path) == tmp_path / "index.html"


def test_single_paper_render_is_a_complete_document() -> None:
    page = HtmlExporter().render(make_paper("arXiv:2610.00001"), ExportContext())
    assert page.startswith("<!doctype html>")
    assert "A Paper About Agents" in page


# ── L8-F：成本板块（趋势 + 本月累计 + 异常）─────────────────────────────
def test_site_has_cost_section_with_trend(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    eng = engine_for(settings.db_path())
    SQLModel.metadata.create_all(eng)
    _seed_reports(tmp_path, [(date(2026, 10, 24), 0.12), (date(2026, 10, 25), 0.30)])

    with Session(eng) as session:
        session.add(make_paper("arXiv:1"))
        session.commit()

    out = tmp_path / "site"
    run_site(out=out, settings=settings, today=TODAY)
    html = (out / "index.html").read_text(encoding="utf-8")

    assert '<section id="cost">' in html
    assert "<svg" in html and "每日 LLM 成本" in html
    assert "本月累计" in html and "0.42" in html  # 0.12 + 0.30
    assert "2026-10-24" in html and "2026-10-25" in html


def test_site_shows_cost_anomaly(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    eng = engine_for(settings.db_path())
    SQLModel.metadata.create_all(eng)
    _seed_reports(
        tmp_path,
        [(date(2026, 10, 25), 0.9)],
        anomalies=["今日 ¥0.9000 > 日限额一半 ¥1.00"],
    )

    out = tmp_path / "site"
    run_site(out=out, settings=settings, today=TODAY)
    html = (out / "index.html").read_text(encoding="utf-8")
    assert "成本异常" in html


def test_site_cost_section_is_idempotent(tmp_path: Path) -> None:
    """★ 同一份 reports ⇒ 同一段 HTML（无时间戳、无随机序）。"""
    settings = _settings(tmp_path)
    eng = engine_for(settings.db_path())
    SQLModel.metadata.create_all(eng)
    _seed_reports(tmp_path, [(date(2026, 10, 24), 0.1), (date(2026, 10, 25), 0.2)])
    out = tmp_path / "site"
    run_site(out=out, settings=settings, today=TODAY)
    first = (out / "index.html").read_bytes()
    run_site(out=out, settings=settings, today=TODAY)
    assert (out / "index.html").read_bytes() == first
