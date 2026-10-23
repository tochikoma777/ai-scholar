"""Obsidian 导出器（设计方案 §6）。

★ 三条最容易违反的硬约束，都在这里：

1. **标记块**（§3.1）：生成区在 `<!-- BEGIN/END GENERATED -->` 之间，
   用户区**永不覆盖**。不实现就是灾难性数据丢失。
   且标记块注释里**不能有时间戳**（否则每次导出都变，与幂等直接冲突）。
2. **frontmatter 读-合并-写回**：保住 `status` 等用户字段；
   ★ `citations` 是观测值，**已有值保持不变**（每次更新会产生海量 diff）。
3. **文件内不留导出时间**：只留日期级的 `content_rev_at`（内容变才更新）；
   导出时间只进 `90-元数据/生成报告.md`。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from pathlib import Path

import yaml

from ai_scholar.export.base import ExportContext, ExportDoc, Exporter, LinkRef
from ai_scholar.store.models import Paper
from ai_scholar.utils.timeutil import today_local

# ★ 标记块注释**不含时间戳**（原稿的 `· 2026-10-17T08:00:00+08:00` 每次都变 ⇒ 与幂等冲突）
BEGIN_MARKER = "<!-- BEGIN GENERATED · 以下内容会被重新生成覆盖 -->"
END_MARKER = "<!-- END GENERATED -->"
USER_SECTION_TITLE = "## 我的笔记"

ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|]')
MULTI_DASH = re.compile(r"-+")
DEFAULT_MAX_LEN = 80
SCHEMA_VERSION = 1

# frontmatter 中**由用户维护**的字段（生成时永不覆盖）
USER_KEYS = frozenset({"status"})
# ★ 观测值：已有值保持不变（只在笔记被重新生成即深度分析/定级触发时才更新）
OBSERVED_KEYS = frozenset({"citations"})


def sanitize_title(title: str, *, max_len: int = DEFAULT_MAX_LEN) -> str:
    """★ 文件名清洗（§6.2）：论文标题几乎必然含冒号，不清洗会写入失败或散落错误路径。

    1. 非法字符 `/ \\ : * ? " < > |` → `-`
    2. 空白 → `-`，连续 `-` 合并
    3. 长度截断（可配）
    """
    cleaned = ILLEGAL_CHARS.sub("-", title or "")
    cleaned = re.sub(r"\s+", "-", cleaned)
    cleaned = MULTI_DASH.sub("-", cleaned).strip("-")
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip("-")
    return cleaned or "untitled"


def short_hash(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8]


def note_name(paper: Paper, *, max_len: int = DEFAULT_MAX_LEN) -> str:
    """笔记名（不含 `.md`）：`YYYY-MM-DD__清洗后的标题`。

    ★ 重名兜底：追加 id 前 8 位短 hash（§6.2 第 5 条）。
    """
    day = (paper.first_seen_at or "")[:10] or "0000-00-00"
    base = sanitize_title(paper.title, max_len=max_len)
    suffix = short_hash(paper.id)
    return f"{day}__{base}-{suffix}"


def dump_frontmatter(fields: dict[str, object]) -> str:
    """按 §6.3 的字段顺序输出 frontmatter（日期型不加引号）。"""
    date_keys = {"published", "first_seen", "content_rev_at"}
    lines = ["---"]
    for key, value in fields.items():
        if value is None:
            lines.append(f"{key}: null")
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, list):
            items = ", ".join(json.dumps(str(v), ensure_ascii=False) for v in value)
            lines.append(f"{key}: [{items}]")
        elif isinstance(value, (int, float)) or key in date_keys and value:
            lines.append(f"{key}: {value}")
        else:
            lines.append(f"{key}: {json.dumps(str(value), ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines)


def _scalar_to_text(value: object) -> object:
    """★ YAML 会把 `2026-10-18` 解析成 `date` 对象；统一收敛回 ISO 字符串。

    不做这一步，读-合并-写回时 `content_rev_at` 会在 `date` 与 `str` 之间来回抖动，
    幂等直接崩掉。
    """
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def parse_frontmatter(text: str) -> tuple[dict[str, object], str]:
    """拆出 frontmatter 与剩余正文（读现有文件用）。"""
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            raw = text[4 : end + 1]
            rest = text[end + 5 :]
            loaded = yaml.safe_load(raw)
            if isinstance(loaded, dict):
                return {k: _scalar_to_text(v) for k, v in loaded.items()}, rest
    return {}, text


def split_generated(body: str) -> tuple[str, str]:
    """拆出生成区与用户区（按标记块）。返回 `(生成区内容, 用户区内容)`。"""
    if BEGIN_MARKER in body and END_MARKER in body:
        start = body.index(BEGIN_MARKER) + len(BEGIN_MARKER)
        end = body.index(END_MARKER)
        return body[start:end], body[end + len(END_MARKER) :]
    return "", body


def render_link_line(ref: LinkRef, note_index: dict[str, str]) -> str:
    """★ 双轨渲染（§6.4 v1.3）：有笔记 → `[[双链]]`；无笔记 → 文本 + 外链（**不建空笔记**）。"""
    note = note_index.get(ref.paper_id)
    if note:
        suffix = f" — {ref.evidence}" if ref.evidence else ""
        return f"- [[{note}]]{suffix}"
    url = ref.url or "（无外链）"
    return f"- ★ {ref.title}（无笔记）— {url}"


class ObsidianExporter(Exporter):
    """把 db 里的论文物化成 Obsidian 笔记。"""

    name = "obsidian"

    def __init__(self, *, filename_max_len: int = DEFAULT_MAX_LEN) -> None:
        self.filename_max_len = filename_max_len

    # ── 路径 ────────────────────────────────────────────────────────────
    def note_path(self, paper: Paper, context: ExportContext) -> Path:
        """`vault/10-文献/{year}/{YYYY-MM-DD__标题}.md`。"""
        year = (paper.first_seen_at or "")[:4] or "0000"
        return (
            Path(context.vault_dir)
            / "10-文献"
            / year
            / f"{note_name(paper, max_len=self.filename_max_len)}.md"
        )

    # ── 渲染 ────────────────────────────────────────────────────────────
    def _generated_body(self, paper: Paper, context: ExportContext) -> str:
        # ★ 空态显式（执行手册 §2.3）：没有内容就显示 `—`，不静默省略区块
        lines = ["", "## 一句话", "—", "", "## 为什么重要", "—", "", "## 方法标签", "—"]
        if context.watchlist_groups:
            # ★ 只打★，不决定是否导出（红线 9）
            stars = ", ".join(context.watchlist_groups)
            lines += ["", "## 命中", f"★ {stars}"]
        lines += ["", "## 关联文献", "### 引用了"]
        if context.links:
            lines += [render_link_line(ref, context.note_index) for ref in context.links]
        else:
            lines += ["（暂无 — 关联数据在 L1 由 S2 填充）"]
        lines += [""]
        return "\n".join(lines)

    def _fields(self, paper: Paper, context: ExportContext) -> dict[str, object]:
        authors = json.loads(paper.authors_json or "[]")
        sources = [paper.source]
        if paper.hf_score is not None and "hf_papers" not in sources:
            sources.append("hf_papers")
        tags = [f"论文/{paper.primary_category}"] if paper.primary_category else []
        return {
            "id": paper.id.replace(":", "-").lower(),
            "title": paper.title,
            "authors": [str(a) for a in authors],
            "published": (paper.published_at or "")[:10] or None,
            "first_seen": (paper.first_seen_at or "")[:10] or None,
            "source": sources,
            "primary_source": paper.primary_source,
            "score": None,  # ★ 本阶段不计算，留 null
            "tier": None,  # 定级属 L3
            "tags": tags,
            "status": "待读",  # ★ 用户区字段，生成时不覆盖（读-合并-写回保住）
            "arxiv_id": paper.arxiv_id,
            "hf_score": paper.hf_score,
            "citations": paper.citation_count,
            "refs_count": paper.refs_count,
            "content_rev_at": None,  # 下面按"内容是否变化"决定
            "llm_mode": context.llm_mode,
            "schema_version": SCHEMA_VERSION,
        }

    def render(self, paper: Paper, context: ExportContext) -> str:
        """渲染完整文件：frontmatter + 标记块（生成区）+ 用户区。

        ★ 若目标文件已存在：**读-合并-写回**，用户区与 `status` 原样保留。
        """
        path = self.note_path(paper, context)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        old_fields, old_body = parse_frontmatter(existing)
        old_generated, old_user = split_generated(old_body)

        new_fields = self._fields(paper, context)
        new_generated = self._generated_body(paper, context)

        # ★ content_rev_at：只有生成区内容变了才更新（日期级，减少 diff）
        if old_generated and old_generated == new_generated:
            new_fields["content_rev_at"] = (
                old_fields.get("content_rev_at") or (paper.first_seen_at or "")[:10]
            )
        else:
            new_fields["content_rev_at"] = today_local().isoformat()

        # ★ 读-合并-写回：保住用户字段与观测值
        for key in USER_KEYS:
            if key in old_fields:
                new_fields[key] = old_fields[key]
        for key in OBSERVED_KEYS:
            if old_fields.get(key) is not None:
                new_fields[key] = old_fields[key]  # 已有值保持不变
        for key, value in old_fields.items():
            if key not in new_fields:
                new_fields[key] = value  # 用户自定义的额外字段一律保留

        user_section = old_user.lstrip("\n")
        if USER_SECTION_TITLE not in user_section:
            user_section = f"{USER_SECTION_TITLE}\n"
        elif not user_section.endswith("\n"):
            user_section += "\n"

        body = f"{BEGIN_MARKER}\n{new_generated}\n{END_MARKER}\n\n{user_section}"
        return f"{dump_frontmatter(new_fields)}\n{body}"

    # ── 首页 / 报告 / links.json ────────────────────────────────────────
    def render_index(
        self,
        papers: list[Paper],
        context: ExportContext,
        *,
        total_in_db: int,
        note_names: dict[str, str],
    ) -> str:
        """`00-首页.md` MOC（★ 也用标记块）。"""
        lines = ["", f"库内论文数：{total_in_db}", f"本次导出：{len(papers)} 篇"]
        lines += ["", "## 最近导出"]
        for paper in papers[:20]:
            name = note_names.get(paper.id) or note_name(paper, max_len=self.filename_max_len)
            star = "★ " if context.watchlist_groups else ""
            lines.append(f"- {star}[[{name}]] — {paper.title[:80]}")
        lines += [""]
        fields = {"title": "AI Scholar · 首页", "schema_version": SCHEMA_VERSION}
        return (
            f"{dump_frontmatter(fields)}\n{BEGIN_MARKER}\n" + "\n".join(lines) + f"{END_MARKER}\n"
        )

    def render_report(
        self,
        *,
        exported: int,
        total_in_db: int,
        llm_mode: str,
        llm_usage_rows: int,
        degraded: dict[str, str],
        export_day: date,
    ) -> str:
        """`90-元数据/生成报告.md`（★ 导出时间只进这里）。

        ★ 为保证"连续两次导出无 diff"，这里的时间是**日期级**；
        秒级时间请查 db 的 `runs.started_at`。
        """
        lines = [
            "",
            f"导出日期：{export_day.isoformat()}",
            f"运行档位：{llm_mode}",
            f"LLM 用量：{llm_usage_rows} 行（off 档必须为 0）",
            f"本次导出条数：{exported}",
            f"库内论文总数：{total_in_db}",
            "",
            "## 降级状态（★ 静默失败最危险，必须显式暴露）",
        ]
        if degraded:
            for key, reason in degraded.items():
                lines.append(f"- {key}：{reason}")
        else:
            lines.append("- 无")
        lines += [
            "",
            "> ★ 本报告只记**日期级**导出时间：秒级会导致重复导出产生 diff。"
            "需要精确时间请查 db 的 `runs.started_at`。",
            "",
        ]
        fields = {"title": "生成报告", "schema_version": SCHEMA_VERSION}
        return (
            f"{dump_frontmatter(fields)}\n{BEGIN_MARKER}\n" + "\n".join(lines) + f"{END_MARKER}\n"
        )

    def write_links_json(self, context: ExportContext, links: list[LinkRef]) -> Path:
        """`90-元数据/links.json`（本阶段为空结构，L1 填数据）。"""
        payload = {
            "schema_version": SCHEMA_VERSION,
            "links": [
                {
                    "src_id": r.paper_id,
                    "link_type": r.link_type,
                    "title": r.title,
                    "url": r.url,
                    "weight": r.weight,
                    "evidence": r.evidence,
                }
                for r in links
            ],
        }
        doc = ExportDoc(
            path=Path(context.vault_dir) / "90-元数据" / "links.json",
            content=json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )
        return self.write(doc)
