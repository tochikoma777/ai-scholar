"""arXiv 正文**临时**抓取（L3b-2）。

★★ 合规红线（设计方案 §1.3 / §3.7.1）：
- 正文**永不入库**：本模块只在内存里返回文本，调用方用完即弃；
  **不写 DB、不写文件、不落 JSONL**。
- 抓不到（无 HTML / 404 / 网络错误）返回 `None`，深度分析退回仅摘要模式。

★ 只抓 arXiv HTML（`https://arxiv.org/html/{id}`），不引入 PDF 解析依赖。

★★ **实测结论（2026-10-25，勿再试）**：
`ar5iv.labs.arxiv.org/abs/{id}` **不能作为回退源** —— 对「有 HTML」和「无 HTML」
的论文它都只返回约 5k 字符的 **arXiv 摘要/元数据页**（导航 + 主题分类），不是正文。
拿它当正文会让 prompt 误以为有全文，比返回 `None` 更糟。
真正文只有 `arxiv.org/html`（抽样 8/8 命中，正文 38k–184k 字符）。

★ 长文处理：超过上限时**中间省略**（保留开头 + 结尾的实验/结论），
  而不是硬砍开头 —— 背景透镜需要的算力/访存数字通常在末尾的实验段。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

import httpx

from ai_scholar.utils.http import HttpClient

ARXIV_HTML_BASE = "https://arxiv.org/html"
# 截断时插入的省略标记（提示模型中间被省略，避免它以为论文只有这么长）
ELISION = "\n\n…（中间省略）…\n\n"
# 头部保留比例（其余给尾部：实验/结论段）
HEAD_RATIO = 0.7
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


def truncate_middle(text: str, max_chars: int) -> str:
    """超限时间**中间省略**：保留开头（引言/方法）+ 结尾（实验/结论）。

    ★ 背景透镜要的算力/访存/通信数字多在末尾实验段，硬砍开头会整段丢失。
    """
    if len(text) <= max_chars:
        return text
    budget = max_chars - len(ELISION)
    if budget <= 0:
        return text[:max_chars]
    head = int(budget * HEAD_RATIO)
    tail = budget - head
    return text[:head] + ELISION + text[-tail:]


async def fetch_arxiv_html(
    http: HttpClient,
    arxiv_id: str | None,
    *,
    max_chars: int = 100000,
    min_chars: int = 2000,
    base_url: str = ARXIV_HTML_BASE,
) -> str | None:
    """抓 arXiv HTML 并抽文本；失败/过短返回 None。**只在内存，不持久化。**"""
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
    # ★ 短于阈值的多半是元数据/占位页 —— 绝不把它伪装成正文
    if len(text) < min_chars:
        return None
    return truncate_middle(text, max_chars)
