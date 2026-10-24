"""L3b-2：arXiv HTML 正文临时抓取测试。"""

from __future__ import annotations

import httpx
import respx

from ai_scholar.collectors.fulltext import extract_text, fetch_arxiv_html
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
        text = await fetch_arxiv_html(http, "2610.00001")
    assert text is not None and "Hello world." in text


@respx.mock
async def test_fetch_arxiv_html_404_returns_none() -> None:
    respx.get(URL).mock(return_value=httpx.Response(404))
    async with HttpClient() as http:
        assert await fetch_arxiv_html(http, "2610.00001") is None


async def test_fetch_arxiv_html_without_id_returns_none() -> None:
    async with HttpClient() as http:
        assert await fetch_arxiv_html(http, None) is None


@respx.mock
async def test_fetch_arxiv_html_truncates() -> None:
    long_html = "<p>" + "x" * 1000 + "</p>"
    respx.get(URL).mock(return_value=httpx.Response(200, text=long_html))
    async with HttpClient() as http:
        text = await fetch_arxiv_html(http, "2610.00001", max_chars=100)
    assert text is not None and len(text) == 100
