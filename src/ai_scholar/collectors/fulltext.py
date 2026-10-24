"""arXiv 正文**临时**抓取（L3b-2）。

★★ 合规红线（设计方案 §1.3 / §3.7.1）：
- 正文**永不入库**：本模块只在内存里返回文本，调用方用完即弃；
  **不写 DB、不写文件、不落 JSONL**。
- 抓不到（无 HTML / 404 / 网络错误）返回 `None`，深度分析退回仅摘要模式。

★ 只抓 arXiv HTML（`https://arxiv.org/html/{id}`），不引入 PDF 解析依赖。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

import httpx

from ai_scholar.utils.http import HttpClient

ARXIV_HTML_BASE = "https://arxiv.org/html"
_SKIP_TAGS = frozenset({"script", "style", "noscript", "svg"})
_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}
)


class _TextExtractor(HTMLParser):
    """从 HTML 里抽可见文本（stdlib，零额外依赖）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def extract_text(html: str) -> str:
    """HTML → 纯文本（压缩空白与空行）。"""
    parser = _TextExtractor()
    parser.feed(html or "")
    text = "".join(parser.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n[ \t]*", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


async def fetch_arxiv_html(
    http: HttpClient,
    arxiv_id: str | None,
    *,
    max_chars: int = 60000,
    base_url: str = ARXIV_HTML_BASE,
) -> str | None:
    """抓 arXiv HTML 并抽文本；失败返回 None。**只在内存，不持久化。**"""
    clean_id = (arxiv_id or "").strip()
    if not clean_id:
        return None
    url = f"{base_url.rstrip('/')}/{clean_id}"
    try:
        response = await http.get(url, source="arxiv_html", rate_limit="1/3s")
    except (httpx.HTTPError, OSError):
        return None
    if response.status_code != 200:
        return None
    text = extract_text(response.text)
    if not text:
        return None
    return text[:max_chars]
