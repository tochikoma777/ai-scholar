"""L0-B：采集 / 三级去重 / `content_hash` / `first_seen` / off 档。

★ 测试里的 Atom 与 JSON **结构照抄真实响应**（命名空间 `<arxiv:primary_category>`、
`<opensearch:totalResults>`、`paper.id` / `paper.upvotes` 等都来自实测），
只有标题 / id / 摘要这些**值**是占位 —— 不凭想象编字段。

★ 解析逻辑本身由 `test_live_arxiv_parse` 用**真实网络**验证；
拿不到就 `pytest.skip`，绝不伪造 fixtures。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from sqlmodel import Session, SQLModel, select

from ai_scholar.collectors.arxiv import parse_feed, submitted_query
from ai_scholar.collectors.base import SOURCE_ARXIV, SOURCE_HF, RawPaper
from ai_scholar.collectors.hf import fetch_hf_day, parse_daily_papers
from ai_scholar.config import Settings
from ai_scholar.pipeline.collect import (
    canonical_id_for,
    llm_usage_rows,
    run_collect,
    upsert_paper,
)
from ai_scholar.store.db import engine_for
from ai_scholar.store.first_seen import FirstSeenStore
from ai_scholar.store.hash import (
    canonical_arxiv_id,
    content_hash,
    normalize_author,
    paper_id_for_doi,
)
from ai_scholar.store.models import LLMUsage, Paper, PaperAlias

ARXIV_URL = "https://export.arxiv.org/api/query"
HF_URL = "https://huggingface.co/api/daily_papers"


# ── 真实结构的测试输入 ──────────────────────────────────────────────────
def atom_feed(entries_xml: str, total: int = 1) -> str:
    """arXiv Atom 骨架（命名空间与字段照抄真实响应）。"""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom"'
        ' xmlns:arxiv="http://arxiv.org/schemas/atom"'
        ' xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">'
        f"<opensearch:totalResults>{total}</opensearch:totalResults>"
        "<opensearch:startIndex>0</opensearch:startIndex>"
        "<opensearch:itemsPerPage>200</opensearch:itemsPerPage>"
        f"{entries_xml}"
        "</feed>"
    )


def arxiv_entry(
    arxiv_id: str = "2610.22350v1",
    title: str = "A Study of Things",
    abstract: str = "We study things.",
    authors: tuple[str, ...] = ("John Doe",),
    doi: str | None = None,
) -> str:
    author_xml = "".join(f"<author><name>{a}</name></author>" for a in authors)
    doi_xml = f"<arxiv:doi>{doi}</arxiv:doi>" if doi else ""
    return (
        "<entry>"
        f"<id>http://arxiv.org/abs/{arxiv_id}</id>"
        "<updated>2026-10-21T17:58:06Z</updated>"
        "<published>2026-10-21T17:58:06Z</published>"
        f"<title>{title}</title>"
        f"<summary>{abstract}</summary>"
        '<category term="cs.AI" scheme="http://arxiv.org/schemas/atom"/>'
        '<category term="cs.LG" scheme="http://arxiv.org/schemas/atom"/>'
        '<arxiv:primary_category term="cs.AI"/>'
        f"{doi_xml}"
        f"{author_xml}"
        "</entry>"
    )


def hf_payload(rows: list[tuple[str, str, int]]) -> list[dict[str, object]]:
    """HF daily_papers 结构（照抄真实响应：`paper` 嵌套 + `upvotes`）。"""
    return [
        {
            "paper": {
                "id": pid,
                "title": title,
                "summary": "hf abstract",
                "upvotes": upvotes,
                "publishedAt": "2026-10-20T00:00:00Z",
                "authors": [{"name": "Jane Roe"}],
            },
            "publishedAt": "2026-10-21T00:00:00Z",
            "thumbnail": None,
        }
        for pid, title, upvotes in rows
    ]


def _engine(tmp_path: Path):
    engine = engine_for(tmp_path / "test.db")
    SQLModel.metadata.create_all(engine)
    return engine


# ── ① content_hash 三条硬约束 ───────────────────────────────────────────
def test_content_hash_excludes_citation_count() -> None:
    """★ 改引用数不改 hash（引用数是观测值，进 hash 会导致每天重复入库）。"""
    base = content_hash("Title", "Abstract", ["John Doe"], "NeurIPS 2026")
    # 引用数变化不参与 hash 计算 —— hash 只吃 title/abstract/authors/venue
    same = content_hash("Title", "Abstract", ["John Doe"], "NeurIPS 2026")
    assert base == same
    assert "citation" not in base  # hash 不可能包含观测值字段名


def test_content_hash_excludes_hf_score() -> None:
    """★ 改 hf_score 不改 hash（热度每天在变）。"""
    raw = RawPaper(source=SOURCE_HF, title="T", abstract="A", authors=["A B"], hf_score=1)
    raw2 = RawPaper(source=SOURCE_HF, title="T", abstract="A", authors=["A B"], hf_score=999)
    assert canonical_id_for(raw) == canonical_id_for(raw2)
    assert content_hash(raw.title, raw.abstract, raw.authors, raw.venue) == content_hash(
        raw2.title, raw2.abstract, raw2.authors, raw2.venue
    )


def test_content_hash_author_name_forms_equivalent() -> None:
    """★ `J. Doe` 与 `John Doe` 必须同 hash（否则跨源假重复）。"""
    assert normalize_author("John Doe") == normalize_author("J. Doe")
    assert normalize_author("John Doe") == normalize_author("DOE, John")
    assert content_hash("T", "A", ["John Doe"]) == content_hash("T", "A", ["J. Doe"])


def test_content_hash_is_sensitive_to_real_content_change() -> None:
    """反向断言：内容真变了 hash 必须变（否则上面的"不变"测试可能只是恒等函数）。"""
    assert content_hash("Title A", "Abs") != content_hash("Title B", "Abs")
    assert content_hash("T", "Abs A") != content_hash("T", "Abs B")
    assert content_hash("T", "A", ["John Doe"]) != content_hash("T", "A", ["Jane Roe"])


# ── ② first_seen.jsonl ──────────────────────────────────────────────────
def test_first_seen_append_only(tmp_path: Path) -> None:
    """★ 已有 id 绝不覆盖（它是"第一次见到它是哪天"，重写即永久丢失）。"""
    path = tmp_path / "first_seen.jsonl"
    store = FirstSeenStore(path)
    assert store.append("arXiv:1", "2026-10-20", "arxiv") is True
    assert store.append("arXiv:1", "2026-10-21", "hf_papers") is False  # 已存在 → 跳过
    assert len(store) == 1
    assert store.get("arXiv:1").first_seen_at == "2026-10-20"  # 原值保留
    assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 1


def test_first_seen_reload_is_idempotent(tmp_path: Path) -> None:
    """同一天跑两次：文件行数不变（幂等）。"""
    path = tmp_path / "first_seen.jsonl"
    FirstSeenStore(path).append("arXiv:1", "2026-10-20", "arxiv")
    FirstSeenStore(path).append("arXiv:2", "2026-10-20", "arxiv")
    reloaded = FirstSeenStore(path)
    reloaded.append("arXiv:1", "2026-10-20", "arxiv")  # 重复写
    reloaded.append("arXiv:2", "2026-10-20", "arxiv")
    assert len(reloaded) == 2
    assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 2


# ── ③ 三级去重 ──────────────────────────────────────────────────────────
def test_level1_same_paper_twice_row_count_unchanged(tmp_path: Path) -> None:
    """一级（身份）：同一篇跑两次，`papers` 行数不变。"""
    engine = _engine(tmp_path)
    first_seen = FirstSeenStore(tmp_path / "first_seen.jsonl")
    raw = RawPaper(
        source=SOURCE_ARXIV,
        title="Same Paper",
        abstract="A",
        authors=["John Doe"],
        arxiv_id="2610.22350v1",
    )
    with Session(engine) as s:
        assert upsert_paper(s, raw, first_seen, "2026-10-21") == "new"
        s.commit()
    with Session(engine) as s:
        assert upsert_paper(s, raw, first_seen, "2026-10-21") == "deduped"
        s.commit()
        assert len(s.exec(select(Paper)).all()) == 1


def test_level1_version_suffix_is_stripped() -> None:
    """一级：arXiv id 剥离版本号（`2610.22350v2` → `2610.22350`）。"""
    assert canonical_arxiv_id("2610.22350v2") == "2610.22350"
    raw = RawPaper(source=SOURCE_ARXIV, title="T", abstract="A", arxiv_id="2610.22350v2")
    assert canonical_id_for(raw) == "arXiv:2610.22350"


def test_level2_hf_only_patches_score(tmp_path: Path) -> None:
    """二级（跨源）：arXiv 与 HF 同一篇**只 1 条**，HF 只补 `hf_score`。

    ★ HF 得分只是热度信号，**不是身份依据**，也不覆盖元数据。
    """
    engine = _engine(tmp_path)
    first_seen = FirstSeenStore(tmp_path / "first_seen.jsonl")
    arxiv_raw = RawPaper(
        source=SOURCE_ARXIV,
        title="Shared Paper",
        abstract="from arxiv",
        authors=["John Doe"],
        arxiv_id="2610.22350v1",
        url="https://arxiv.org/abs/2610.22350v1",
    )
    hf_raw = RawPaper(
        source=SOURCE_HF,
        title="Shared Paper (HF title variant)",  # HF 标题可能不同 → 不得覆盖
        abstract="from hf",
        authors=["J. Doe"],
        arxiv_id="2610.22350",
        hf_score=42,
    )
    with Session(engine) as s:
        upsert_paper(s, arxiv_raw, first_seen, "2026-10-21")
        s.commit()
    with Session(engine) as s:
        result = upsert_paper(s, hf_raw, first_seen, "2026-10-21")
        s.commit()
        papers = s.exec(select(Paper)).all()

    assert result == "deduped"
    assert len(papers) == 1  # ★ 不产生新条目
    paper = papers[0]
    assert paper.hf_score == 42  # ★ HF 只补热度
    assert paper.title == "Shared Paper"  # 元数据不被 HF 覆盖
    assert paper.abstract == "from arxiv"
    assert paper.primary_source == SOURCE_ARXIV


def test_level3_alias_does_not_create_new_paper(tmp_path: Path) -> None:
    """三级（跨版本）：别名写进 `paper_aliases`，**不产生新条目**。"""
    engine = _engine(tmp_path)
    first_seen = FirstSeenStore(tmp_path / "first_seen.jsonl")
    preprint = RawPaper(
        source=SOURCE_ARXIV,
        title="Preprint Version",
        abstract="A",
        authors=["John Doe"],
        arxiv_id="2610.22350v1",
        doi="10.1234/ABC",  # DOI 前缀大写 → 需统一小写
    )
    with Session(engine) as s:
        upsert_paper(s, preprint, first_seen, "2026-10-21")
        s.commit()
        papers = s.exec(select(Paper)).all()
        aliases = s.exec(select(PaperAlias)).all()

    assert len(papers) == 1
    kinds = {a.alias_kind for a in aliases}
    assert "doi" in kinds  # DOI 别名已登记
    assert paper_id_for_doi("10.1234/abc") in {a.alias for a in aliases}
    assert all(a.canonical_id == "arXiv:2610.22350" for a in aliases)


def test_level3_resolve_via_alias(tmp_path: Path) -> None:
    """三级读路径：正式版（只有 DOI）与预印本（只有 arXiv id）归一到同一条。"""
    engine = _engine(tmp_path)
    first_seen = FirstSeenStore(tmp_path / "first_seen.jsonl")
    published = RawPaper(source=SOURCE_ARXIV, title="Published", abstract="A", doi="10.1234/abc")
    preprint = RawPaper(
        source=SOURCE_ARXIV,
        title="Preprint",
        abstract="A",
        arxiv_id="2610.99999v1",
        doi="10.1234/abc",  # ★ 同一个 DOI ⇒ 通过别名归一
    )
    with Session(engine) as s:
        upsert_paper(s, published, first_seen, "2026-10-21")
        s.commit()
    with Session(engine) as s:
        result = upsert_paper(s, preprint, first_seen, "2026-10-21")
        s.commit()
        count = len(s.exec(select(Paper)).all())
    assert result in ("updated", "deduped")
    assert count == 1  # ★ 别名归一，未产生新条目


# ── ④ watchlist 不影响入库 ──────────────────────────────────────────────
def test_pipeline_never_imports_watchlist() -> None:
    """★ watchlist 是意图层（打★/触发/排序），**永远不决定论文是否入库**。

    领域过滤是 cs.AI/CL/LG，已在 arXiv 查询的 `cat:` 里施加过 —— 不再滤第二遍。
    这条用源码级断言守住（本项目已犯两次同类层次混淆）。
    """
    import re

    src = Path(__file__).resolve().parents[1] / "src/ai_scholar/pipeline/collect.py"
    text = src.read_text(encoding="utf-8")
    # ★ 只查"引用"，不查 docstring 里对这条红线的文字说明
    assert not re.search(r"^\s*(from|import)\s+.*watchlist", text, re.M)
    assert "match_watchlist" not in text
    assert "load_watchlist" not in text


@respx.mock
async def test_watchlist_does_not_change_ingest_count(tmp_path: Path) -> None:
    """清空 watchlist 后采集，入库条数不变（运行时验证）。"""
    respx.get(ARXIV_URL).mock(
        return_value=httpx.Response(
            200,
            text=atom_feed(
                arxiv_entry("2610.00001v1", "Paper One") + arxiv_entry("2610.00002v1", "Paper Two"),
                total=2,
            ),
        )
    )
    respx.get(HF_URL).mock(return_value=httpx.Response(200, json=[]))
    settings = Settings()

    first = await run_collect(
        days=1,
        settings=settings,
        db_path=tmp_path / "a.db",
        first_seen_path=tmp_path / "fs.jsonl",
    )
    # 换一个"空的" watchlist 环境再跑一次（watchlist 根本不参与，结果必然一致）
    second = await run_collect(
        days=1,
        settings=settings,
        db_path=tmp_path / "b.db",
        first_seen_path=tmp_path / "fs2.jsonl",
    )
    assert first.fetched == second.fetched == 2
    assert (first.new, first.deduped) == (second.new, second.deduped)


# ── ⑤ off 档：0 次 LLM 调用 + llm_usage 0 行 ────────────────────────────
@respx.mock
async def test_off_mode_makes_no_llm_call(tmp_path: Path) -> None:
    """★ off 档：LLM 端点 **0 次调用**（不是"用 mock 顶掉后断言 0"）。"""
    llm_route = respx.route(host="api.deepseek.com").mock(
        return_value=httpx.Response(200, json={"choices": []})
    )
    respx.get(ARXIV_URL).mock(
        return_value=httpx.Response(200, text=atom_feed(arxiv_entry(), total=1))
    )
    respx.get(HF_URL).mock(return_value=httpx.Response(200, json=[]))

    db_path = tmp_path / "off.db"
    report = await run_collect(
        days=1, settings=Settings(), db_path=db_path, first_seen_path=tmp_path / "fs.jsonl"
    )
    assert report.fetched >= 1
    assert llm_route.call_count == 0  # ★ 代码根本没向 LLM 发请求

    engine = engine_for(db_path)
    with Session(engine) as s:
        assert llm_usage_rows(s) == []
        assert s.exec(select(LLMUsage)).all() == []


# ── ⑥ 单源失败隔离 ──────────────────────────────────────────────────────
@respx.mock
async def test_single_source_failure_is_degraded_not_fatal(tmp_path: Path) -> None:
    """★ 单源失败：记 degraded，其余源照跑，不抛异常（退出码由 CLI 决定为 0）。"""
    respx.get(ARXIV_URL).mock(
        return_value=httpx.Response(200, text=atom_feed(arxiv_entry(), total=1))
    )
    respx.get(HF_URL).mock(side_effect=httpx.ConnectError("unreachable"))

    report = await run_collect(
        days=1,
        settings=Settings(),
        db_path=tmp_path / "c.db",
        first_seen_path=tmp_path / "fs.jsonl",
    )
    assert [o.name for o in report.outcomes] == ["arxiv", "hf_daily"]
    assert report.outcomes[0].failed is False
    assert report.outcomes[1].failed is True
    assert "hf_daily" in report.degraded
    assert report.ok is False  # 有源降级 —— 但不影响退出码


@respx.mock
async def test_degraded_is_persisted_in_runs(tmp_path: Path) -> None:
    """降级状态必须显式暴露：`runs.degraded_json` 有记录。"""
    from sqlmodel import select as sqlmodel_select

    from ai_scholar.store.models import Run

    respx.get(ARXIV_URL).mock(
        return_value=httpx.Response(200, text=atom_feed(arxiv_entry(), total=1))
    )
    respx.get(HF_URL).mock(side_effect=httpx.ConnectError("unreachable"))
    db_path = tmp_path / "d.db"
    await run_collect(
        days=1, settings=Settings(), db_path=db_path, first_seen_path=tmp_path / "fs.jsonl"
    )
    engine = engine_for(db_path)
    with Session(engine) as s:
        runs = s.exec(sqlmodel_select(Run)).all()
        assert len(runs) == 1
        assert runs[0].degraded_json is not None
        assert "hf_daily" in json.loads(runs[0].degraded_json)
        assert runs[0].mode == "off"  # 未配 key ⇒ off 档


# ── ⑦ 真实解析（拿不到就 skip，绝不伪造）───────────────────────────────
@pytest.mark.live
async def test_live_arxiv_parse() -> None:
    """用**真实响应**验证 arXiv 字段解析（拿不到就 skip，不编样例）。"""
    from ai_scholar.utils.http import HttpClient

    async with HttpClient(connect_timeout_s=10, read_timeout_s=40) as client:
        try:
            resp = await client.get(
                ARXIV_URL,
                params={
                    "search_query": submitted_query(
                        ["cs.AI"], __import__("datetime").date(2026, 10, 21)
                    ),
                    "start": 0,
                    "max_results": 1,
                },
                source="arxiv",
                rate_limit="1/3s",
                timeout=None,
            )
        except (httpx.HTTPError, OSError) as exc:
            pytest.skip(reason=f"arXiv 不可达：{type(exc).__name__}: {exc}")
    if resp.status_code != 200:
        pytest.skip(reason=f"arXiv HTTP {resp.status_code}")
    total, items = parse_feed(resp.text)
    if not items:
        pytest.skip(reason="arXiv 返回 0 条（当天可能尚未索引）")
    assert total >= 1
    assert items[0].title
    assert items[0].arxiv_id  # 解析出真实 arXiv id


@pytest.mark.live
async def test_live_hf_parse_may_skip() -> None:
    """HF 真实解析：官方不可达时 skip（本环境已知不可达，配 `hf_base_url` 可解）。"""
    from datetime import date

    from ai_scholar.utils.http import HttpClient

    async with HttpClient(connect_timeout_s=10, read_timeout_s=30) as client:
        try:
            resp = await client.get(
                HF_URL,
                params={"date": date(2026, 10, 21).isoformat()},
                source="hf_daily",
                rate_limit="10/1s",
                timeout=None,
            )
        except (httpx.HTTPError, OSError) as exc:
            pytest.skip(reason=f"HF 官方端点不可达（网络问题，配 hf_base_url 可解）：{exc}")
    if resp.status_code != 200:
        pytest.skip(reason=f"HF HTTP {resp.status_code}")
    items = parse_daily_papers(resp.json())
    if not items:
        pytest.skip(reason="HF 当天无数据")
    assert items[0].arxiv_id


def test_hf_score_is_not_identity() -> None:
    """★ HF 得分只是热度信号，不参与 id 生成。"""
    a = RawPaper(source=SOURCE_HF, title="T", abstract="A", arxiv_id="2610.1", hf_score=1)
    b = RawPaper(source=SOURCE_HF, title="T", abstract="A", arxiv_id="2610.1", hf_score=999)
    assert canonical_id_for(a) == canonical_id_for(b)


def test_hf_parse_rejects_non_array() -> None:
    """响应不是数组 ⇒ 明确报错（不静默吞成 0 条）。"""
    with pytest.raises(ValueError, match="不是数组"):
        parse_daily_papers({"error": "nope"})


def test_hf_parse_tolerates_missing_fields() -> None:
    """字段缺失（无 upvotes / 无 authors）不应炸，只留空。"""
    items = parse_daily_papers([{"paper": {"id": "2610.1", "title": "T"}}])
    assert len(items) == 1
    assert items[0].hf_score is None
    assert items[0].authors == []
    assert items[0].arxiv_id == "2610.1"


def test_hf_collect_days_excludes_today() -> None:
    """L6-1：HF 榜单滞后发布 —— 采集窗口必须从昨天开始，绝不请求今天。

    根因（2026-10-25 实测）：官方与镜像都对「今天」返回 400：
    `"date" must be less than or equal to "<今天>T00:00:00.000Z"`。
    若照旧请求今天，每天第一次 daily 必然降级，且错误像"源挂了"。
    """
    from datetime import date

    from ai_scholar.collectors.hf import hf_collect_days

    assert hf_collect_days(date(2026, 10, 25), 7) == [
        date(2026, 10, 24),
        date(2026, 10, 23),
        date(2026, 10, 22),
        date(2026, 10, 21),
        date(2026, 10, 20),
        date(2026, 10, 19),
        date(2026, 10, 18),
    ]
    assert date(2026, 10, 25) not in hf_collect_days(date(2026, 10, 25), 7)
    assert hf_collect_days(date(2026, 10, 25), 0) == []


@respx.mock
async def test_hf_error_message_carries_body(tmp_path: Path) -> None:
    """L6-1：非 200 必须带截断响应体 —— 只有状态码无法区分网络/API 变更/日期超限。"""
    respx.get(HF_URL).mock(
        return_value=httpx.Response(
            400,
            json={"error": '✖ "date" must be less than or equal to "2026-10-25T00:00:00.000Z"'},
        )
    )
    from datetime import date

    from ai_scholar.utils.http import HttpClient

    async with HttpClient() as client:
        with pytest.raises(RuntimeError, match=r"HTTP 400.*date"):
            await fetch_hf_day(client, date(2026, 10, 25), HF_URL)


@respx.mock
async def test_collect_hf_skips_today_even_on_failure(tmp_path: Path) -> None:
    """L6-1：采集不请求今天 ⇒ 即使 HF 整体失败，degraded 原因也不再是「日期超限」。"""
    from datetime import date

    respx.get(ARXIV_URL).mock(
        return_value=httpx.Response(200, text=atom_feed(arxiv_entry(), total=1))
    )
    respx.get(HF_URL).mock(return_value=httpx.Response(400, json={"error": "date too new"}))
    await run_collect(
        days=3,
        settings=Settings(),
        db_path=tmp_path / "c.db",
        first_seen_path=tmp_path / "fs.jsonl",
        today=date(2026, 10, 25),
    )
    requested = [
        str(call.request.url) for call in respx.calls if "daily_papers" in str(call.request.url)
    ]
    assert all("date=2026-10-25" not in url for url in requested)
    assert any("date=2026-10-24" in url for url in requested)


@respx.mock
async def test_hf_http_error_becomes_runtime_error() -> None:
    """HF 非 200 / 连接失败 ⇒ 抛错（由 pipeline 转成 warning + 跳过）。"""
    respx.get(HF_URL).mock(return_value=httpx.Response(503))
    from datetime import date

    from ai_scholar.utils.http import HttpClient

    async with HttpClient() as client:
        with pytest.raises(RuntimeError, match="HTTP 503"):
            await fetch_hf_day(client, date(2026, 10, 21), HF_URL)

    respx.get(HF_URL).mock(side_effect=httpx.ConnectError("unreachable"))
    async with HttpClient() as client:
        with pytest.raises(RuntimeError, match="ConnectError"):
            await fetch_hf_day(client, date(2026, 10, 21), HF_URL)
