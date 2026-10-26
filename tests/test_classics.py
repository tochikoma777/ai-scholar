"""L7-5：经典种子清单（`config/classics.yaml`）+ arXiv `id_list` 直采。

★ 三条必须守住：
1. 清单**存在且被消费**（经验固化 E1：不留死配置）—— 本文件 + 深度分析经典侧都是消费方。
2. `id_list` 直采对**不存在的 id 静默跳过** ⇒ 未命中必须显式返回，不能装作全采到。
3. 清单 id 与标题的核对靠 `scripts/probe_classics.py` 用真实 API 实测（D1），
   本文件只测**接线逻辑**，不伪造"清单内容正确"的结论。
"""

from __future__ import annotations

import httpx
import pytest
import respx
from sqlmodel import Session, SQLModel, select

from ai_scholar.collectors.arxiv import fetch_arxiv_ids
from ai_scholar.config import load_classics
from ai_scholar.pipeline.collect import run_collect
from ai_scholar.store.models import Paper
from ai_scholar.utils.http import HttpClient

ARXIV_URL = "https://export.arxiv.org/api/query"


def _feed(*ids: str) -> str:
    entries = "".join(
        "<entry>"
        f"<id>http://arxiv.org/abs/{i}v1</id>"
        "<published>2017-06-12T00:00:00Z</published>"
        f"<title>Classic Paper {i}</title>"
        "<summary>We propose something classic.</summary>"
        "<author><name>A. Author</name></author>"
        '<category term="cs.LG" scheme="http://arxiv.org/schemas/atom"/>'
        '<arxiv:primary_category term="cs.LG"/>'
        "</entry>"
        for i in ids
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom"'
        ' xmlns:arxiv="http://arxiv.org/schemas/atom"'
        ' xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">'
        f"<opensearch:totalResults>{len(ids)}</opensearch:totalResults>"
        f"{entries}</feed>"
    )


# ── 清单加载 ────────────────────────────────────────────────────────────
def test_seed_list_is_loaded_and_has_ids() -> None:
    """★ 清单必须能被读到且不为空（否则"经典获取"是空话）。"""
    classics = load_classics()
    assert classics.ids(), "config/classics.yaml 为空 —— 经典获取没有种子"
    assert len(set(classics.ids())) == len(classics.ids()), "清单内有重复 id"


def test_seed_list_group_of() -> None:
    classics = load_classics()
    first = classics.ids()[0]
    assert classics.group_of(first)
    assert classics.group_of("0000.00000") is None


def test_seed_list_missing_file_is_empty(tmp_path) -> None:
    """清单缺失 ⇒ 空配置（可选项，不是错误）。"""
    from ai_scholar.config import load_classics as _load

    assert _load(tmp_path).ids() == []


# ── id_list 直采 ────────────────────────────────────────────────────────
async def test_fetch_arxiv_ids_uses_id_list() -> None:
    async with respx.mock(assert_all_called=True) as mock:
        route = mock.get(ARXIV_URL).mock(
            return_value=httpx.Response(200, text=_feed("1706.03762", "1810.04805"))
        )
        async with HttpClient() as client:
            items, missing = await fetch_arxiv_ids(client, ["1706.03762", "1810.04805"], ARXIV_URL)
    request = route.calls[0].request
    assert "id_list=" in str(request.url), "必须用 id_list 批量直采（不是 search_query）"
    assert "1706.03762%2C1810.04805" in str(request.url)
    assert len(items) == 2
    assert missing == []


async def test_fetch_arxiv_ids_reports_missing() -> None:
    """★ 请求 3 个只回 2 个 ⇒ 未命中必须显式报出来（arXiv 是静默跳过）。"""
    async with respx.mock(assert_all_called=True) as mock:
        mock.get(ARXIV_URL).mock(return_value=httpx.Response(200, text=_feed("1706.03762")))
        async with HttpClient() as client:
            _items, missing = await fetch_arxiv_ids(client, ["1706.03762", "9999.99999"], ARXIV_URL)
    assert missing == ["9999.99999"]


async def test_fetch_arxiv_ids_rejects_non_200() -> None:
    """★ 400 不重试，直接带响应体报错（E5：错误信息要能自证）。"""
    async with respx.mock(assert_all_called=True) as mock:
        mock.get(ARXIV_URL).mock(return_value=httpx.Response(400, text="bad id_list"))
        async with HttpClient() as client:
            with pytest.raises(RuntimeError, match="400"):
                await fetch_arxiv_ids(client, ["1706.03762"], ARXIV_URL)


# ── collect --classics 接线 ─────────────────────────────────────────────
async def test_collect_classics_ingests(tmp_path, monkeypatch) -> None:
    """`collect --classics` 必须真的把清单里的论文写进库（不是只打印一句）。"""
    monkeypatch.setenv("AI_SCHOLAR_ROOT", str(tmp_path))
    seeds = ["1706.03762", "1810.04805", "2106.09685"]
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "classics.yaml").write_text(
        "version: 1\ngroups:\n  - name: t\n    papers:\n"
        + "".join(f'      - {{ id: "{i}", note: "n", title: "t" }}\n' for i in seeds),
        encoding="utf-8",
    )

    async with respx.mock(assert_all_called=True) as mock:
        mock.get(ARXIV_URL).mock(return_value=httpx.Response(200, text=_feed(*seeds)))
        report = await run_collect(classics=True, db_path=tmp_path / "db.sqlite3")

    assert report.fetched == len(seeds), "清单条目应全部入库"
    assert report.new == len(seeds)
    assert not report.degraded, "全部命中时不该有降级"

    engine = __import__("sqlalchemy").create_engine(f"sqlite:///{tmp_path / 'db.sqlite3'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        rows = list(session.exec(select(Paper)).all())
    assert {r.arxiv_id for r in rows} == set(seeds)


async def test_collect_classics_reports_missing_ids(tmp_path, monkeypatch) -> None:
    """★ 清单里有拉不到的 id ⇒ 必须进 degraded（否则"49 条"变"46 条"无人察觉）。"""
    monkeypatch.setenv("AI_SCHOLAR_ROOT", str(tmp_path))
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "classics.yaml").write_text(
        'version: 1\ngroups:\n  - name: t\n    papers:\n      - { id: "1706.03762" }\n'
        '      - { id: "9999.99999" }\n',
        encoding="utf-8",
    )
    async with respx.mock(assert_all_called=True) as mock:
        mock.get(ARXIV_URL).mock(return_value=httpx.Response(200, text=_feed("1706.03762")))
        report = await run_collect(classics=True, db_path=tmp_path / "db.sqlite3")
    assert "classics_missing" in report.degraded
    assert "9999.99999" in report.degraded["classics_missing"]

    # 3) 真实清单本身也要能被读到（防"测试绿了但仓库清单是空的"）
    assert load_classics().ids(), "仓库 config/classics.yaml 为空"


async def test_fetch_arxiv_ids_retries_on_429(monkeypatch) -> None:
    """★ 回归（2026-10-26 Actions 实测）：`collect` 刚跑完，classics 立刻请求被 429。

    arXiv 的 429 会丢掉**整批**清单 ⇒ 必须自己退避重试，不能只依赖 HttpClient 的短重试。
    """
    from ai_scholar import collectors

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(collectors.arxiv.asyncio, "sleep", fake_sleep)

    async with respx.mock() as mock:
        route = mock.get(ARXIV_URL)
        route.side_effect = [
            httpx.Response(429, text="Rate exceeded"),
            httpx.Response(429, text="Rate exceeded"),
            httpx.Response(200, text=_feed("1706.03762")),
        ]
        # ★ max_retries=1：关掉 HttpClient 的通用短重试，专测**我们自己的**长退避
        async with HttpClient(max_retries=1) as client:
            items, missing = await fetch_arxiv_ids(client, ["1706.03762"], ARXIV_URL)

    assert len(items) == 1 and missing == []
    # ★ 只断言"我们自己的退避"出现了（HttpClient 内部也会 sleep，会混进同一个列表）
    assert 20.0 in sleeps and 40.0 in sleeps, f"缺少 20s/40s 退避，实际 {sleeps}"


async def test_fetch_arxiv_ids_gives_up_after_backoff(monkeypatch) -> None:
    """退避用完仍 429 ⇒ 明确报错（带响应体），不静默返回 0 条。"""
    from ai_scholar import collectors

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(collectors.arxiv.asyncio, "sleep", no_sleep)

    async with respx.mock() as mock:
        mock.get(ARXIV_URL).mock(return_value=httpx.Response(429, text="Rate exceeded"))
        async with HttpClient(max_retries=1) as client:
            with pytest.raises(RuntimeError, match="429"):
                await fetch_arxiv_ids(client, ["1706.03762"], ARXIV_URL)
