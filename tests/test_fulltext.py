"""L3b-2：arXiv HTML 正文临时抓取测试。"""

from __future__ import annotations

import httpx
import respx

from ai_scholar.collectors.fulltext import extract_text, fetch_arxiv_html, truncate_middle
from ai_scholar.utils.http import HttpClient

URL = "https://arxiv.org/html/2610.00001"
HTML = (
    "<html><body><h1>Title</h1><p>Hello world.</p>"
    "<script>evil()</script><style>.x{}</style></body></html>"
)


def test_extract_text_drops_script_and_style() -> None:
    text = extract_text(HTML)
    assert "Title" in text
    assert "Hello world." in text
    assert "evil" not in text
    assert ".x{}" not in text


@respx.mock
async def test_fetch_arxiv_html_ok() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, text=HTML))
    async with HttpClient() as http:
        # 测试用的 HTML 很短，显式放宽最小长度（真实正文 38k+ 字符）
        text = await fetch_arxiv_html(http, "2610.00001", min_chars=10)
    assert text is not None and "Hello world." in text


@respx.mock
async def test_fetch_arxiv_html_404_returns_none() -> None:
    respx.get(URL).mock(return_value=httpx.Response(404))
    async with HttpClient() as http:
        assert await fetch_arxiv_html(http, "2610.00001") is None


async def test_fetch_arxiv_html_without_id_returns_none() -> None:
    async with HttpClient() as http:
        assert await fetch_arxiv_html(http, None) is None


def test_truncate_middle_keeps_head_and_tail() -> None:
    """★ 超限时间中间省略：保开头（引言）+ 结尾（实验/结论），丢弃中段。"""
    text = "H" * 500 + "M" * 500 + "T" * 500
    out = truncate_middle(text, 400)
    assert out.startswith("H")
    assert out.endswith("T")
    assert "…（中间省略）…" in out
    assert "M" not in out
    assert len(out) <= 400


def test_truncate_middle_returns_short_text_unchanged() -> None:
    assert truncate_middle("abc", 100) == "abc"


@respx.mock
async def test_fetch_arxiv_html_rejects_metadata_stub() -> None:
    """★ 短于阈值的元数据/占位页（如 ar5iv 只给 abs 页）绝不冒充正文。"""
    stub = "<html><body><p>Title</p><p>Abstract only.</p></body></html>"
    respx.get(URL).mock(return_value=httpx.Response(200, text=stub))
    async with HttpClient() as http:
        assert await fetch_arxiv_html(http, "2610.00001", min_chars=2000) is None


@respx.mock
async def test_fetch_arxiv_html_truncates_middle() -> None:
    long_html = "<p>" + "H" * 500 + "</p><p>" + "M" * 500 + "</p><p>" + "T" * 500 + "</p>"
    respx.get(URL).mock(return_value=httpx.Response(200, text=long_html))
    async with HttpClient() as http:
        text = await fetch_arxiv_html(http, "2610.00001", max_chars=300, min_chars=10)
    assert text is not None
    assert text.startswith("H") and text.endswith("T")
    assert "…（中间省略）…" in text
    assert len(text) <= 300
