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
from typing import Any

import yaml

from ai_scholar.export.base import (
    ExportContext,
    ExportDoc,
    Exporter,
    LinkEdge,
    LinkRef,
    TopicEntry,
)
from ai_scholar.store.models import LINK_TYPE_BIBCOUPLING, LINK_TYPE_SIMILARITY, Paper
from ai_scholar.utils.timeutil import DEFAULT_TZ, format_local, today_local

# ★ 标记块注释**不含时间戳**（原稿的 `· 2026-10-17T08:00:00+08:00` 每次都变 ⇒ 与幂等冲突）
BEGIN_MARKER = "<!-- BEGIN GENERATED · 以下内容会被重新生成覆盖 -->"
END_MARKER = "<!-- END GENERATED -->"
USER_SECTION_TITLE = "## 我的笔记"
# ★ Daily Note 的用户区：待办勾选框是**用户状态**，必须放在标记块外，永不重生成
DAILY_USER_TITLE = "## 我的笔记摘要"
DAILY_USER_DEFAULT = "## 我的笔记摘要\n\n## 待办\n- [ ] "

ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|]')
MULTI_DASH = re.compile(r"-+")
DEFAULT_MAX_LEN = 80
DEFAULT_LINKS_MAX = 10  # 「关联文献」每类最多显示条数
SCHEMA_VERSION = 1
# ★ `links.json` 独立版本：L7-7 把空壳换成有向边（src/dst），结构变了 ⇒ 2
LINKS_SCHEMA_VERSION = 2

# frontmatter 中**由用户维护**的字段（生成时永不覆盖）
USER_KEYS = frozenset({"status"})
# ★ 观测值：已有值保持不变（只在笔记被重新生成即深度分析/定级触发时才更新）
#   `refs_count` 与 `citations` 同类：L1-B 的 signals 采样会刷新它们，
#   若每次导出都跟着变 ⇒ 全库笔记反复产生 diff（`--check --all` 直接红）。
OBSERVED_KEYS = frozenset({"citations", "refs_count"})


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


def topic_note_path(vault_dir: Path, topic: str) -> Path:
    """`20-主题/{topic}.md`（★ 组名也走 `sanitize_title`，防止非法字符写盘失败）。"""
    return Path(vault_dir) / "20-主题" / f"{sanitize_title(topic)}.md"


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
        elif isinstance(value, dict):
            # ★ 分项分（`score`）必须**分列多行**，绝不合成一个数字（红线 11）
            lines.append(f"{key}:")
            for sub_key, sub_value in value.items():
                if sub_value is None:
                    lines.append(f"  {sub_key}: null")
                elif isinstance(sub_value, bool):
                    lines.append(f"  {sub_key}: {'true' if sub_value else 'false'}")
                elif isinstance(sub_value, (int, float)):
                    lines.append(f"  {sub_key}: {sub_value}")
                else:
                    lines.append(f"  {sub_key}: {json.dumps(str(sub_value), ensure_ascii=False)}")
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
    """★ 双轨渲染（§6.4 v1.3）：有笔记 → `[[双链]]`；无笔记 → 文本 + 外链（**不建空笔记**）。

    ★ 相似 / 耦合的 `evidence`（`相似 0.42` / `共现 4 篇参考文献`）**是这条边存在的唯一解释**
      （设计方案 §5.4「不可解释的关联不要用」），双轨**都要显示**；
      引用边的 `evidence` 是 `s2_references` 这类机器标记 —— 引用是事实，不显示。
    ★ L3a：强相似边的 `interpretation`（LLM 一句解读）追加在 evidence 之后。
    """
    note = note_index.get(ref.paper_id)
    reason_parts: list[str] = []
    if ref.link_type in (LINK_TYPE_SIMILARITY, LINK_TYPE_BIBCOUPLING) and ref.evidence:
        reason_parts.append(ref.evidence)
    if ref.interpretation:
        reason_parts.append(ref.interpretation)
    reason = f" — {'。'.join(reason_parts)}" if reason_parts else ""
    if note:
        return f"- [[{note}]]{reason}"
    url = ref.url or "（无外链）"
    return f"- ★ {ref.title}（无笔记）— {url}{reason}"


def _link_sort_key(ref: LinkRef, note_index: dict[str, str]) -> tuple[int, str, float]:
    """★ 排序：有笔记的排前；同类内按 `published_at`、再按 `weight`。"""
    return (1 if ref.paper_id in note_index else 0, ref.published_at or "", ref.weight or 0.0)


def render_link_section(
    title: str,
    refs: list[LinkRef],
    note_index: dict[str, str],
    *,
    max_items: int = DEFAULT_LINKS_MAX,
    empty_hint: str = "（暂无）",
) -> list[str]:
    """渲染「关联文献」的一类（引用了 / 被引用）。★ 空态显式，不静默省略。"""
    if not refs:
        return [f"### {title}", empty_hint, ""]
    ordered = sorted(refs, key=lambda r: _link_sort_key(r, note_index), reverse=True)
    lines = [f"### {title}"]
    lines += [render_link_line(ref, note_index) for ref in ordered[:max_items]]
    if len(ordered) > max_items:
        lines.append(f"（还有 {len(ordered) - max_items} 篇）")
    lines.append("")
    return lines


def _score_fields(score: Any | None) -> dict[str, float | None] | None:
    """把 `ScoreBreakdown` 转成分项字典（缺失分量显式 `null`，**不补 0**）。"""
    if score is None:
        return None
    return {
        "heat": getattr(score, "heat", None),
        "cross_source": getattr(score, "cross_source", None),
        "topic": getattr(score, "topic", None),
    }


class ObsidianExporter(Exporter):
    """把 db 里的论文物化成 Obsidian 笔记。"""

    name = "obsidian"

    def __init__(
        self,
        *,
        filename_max_len: int = DEFAULT_MAX_LEN,
        links_max_per_section: int = DEFAULT_LINKS_MAX,
    ) -> None:
        self.filename_max_len = filename_max_len
        self.links_max_per_section = links_max_per_section

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
        one_line = paper.one_line or "—"
        why = paper.why or "—"
        tags = json.loads(paper.method_tags_json or "[]")
        tags_text = " ".join(f"`{t}`" for t in tags) if tags else "—"
        lines = ["", "## 一句话", one_line, "", "## 为什么重要", why, "", "## 方法标签", tags_text]
        if paper.deep_analysis_md:
            # ★ L3b-2：深度分析是系统产物，放在生成区内（可重算）
            lines += ["", paper.deep_analysis_md.strip()]
        if context.watchlist_groups:
            # ★ 只打★，不决定是否导出（红线 9）
            stars = ", ".join(context.watchlist_groups)
            lines += ["", "## 命中", f"★ {stars}"]
        lines += ["", "## 关联文献"]
        lines += render_link_section(
            "引用了",
            context.out_links,
            context.note_index,
            max_items=self.links_max_per_section,
            empty_hint="（暂无 — 引用数据由 `ai-scholar enrich` 从 S2 采集）",
        )
        lines += render_link_section(
            "被引用",
            context.in_links,
            context.note_index,
            max_items=self.links_max_per_section,
            empty_hint="（暂无 — 需等引用它的论文入库后由出边反向生成）",
        )
        # ★ 对称关联只在**有数据时**出现：避免给整个 vault 制造无意义 diff
        if context.similar:
            lines += render_link_section(
                "相似",
                context.similar,
                context.note_index,
                max_items=self.links_max_per_section,
            )
        if context.coupled:
            lines += render_link_section(
                "文献耦合",
                context.coupled,
                context.note_index,
                max_items=self.links_max_per_section,
            )
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
            # ★ L7-8：客观信号分 **分项落**（热度 / 跨源 / 主题），★ 永不合成总分（红线 11）
            "score": _score_fields(context.score),
            "tier": paper.tier,  # ★ L3b-1 定级：1/2/3；未定级 null
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
        #   标记块两侧的换行是**框架**（写入时固定拼 `\n{body}\n`），不属于内容，
        #   比较前必须剥掉 —— 否则跨天后即使内容没变也会刷新日期（--check 会误报）。
        if old_generated.strip("\n") and old_generated.strip("\n") == new_generated.strip("\n"):
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
    def render_daily_note(self, existing: str, *, day: str, body_lines: list[str]) -> str:
        """Daily Note（`40-日志/YYYY-MM-DD.md`，§6.6）。

        ★ 生成区在标记块内；「我的笔记摘要 / 待办」在标记块外 —— 勾选框是用户状态，
          **永不覆盖、永不重做**。
        """
        _old_fields, old_body = parse_frontmatter(existing)
        _old_generated, old_user = split_generated(old_body)
        user = old_user.lstrip("\n")
        if DAILY_USER_TITLE not in user:
            user = f"{DAILY_USER_DEFAULT}\n"
        elif not user.endswith("\n"):
            user += "\n"
        body = f"{BEGIN_MARKER}\n" + "\n".join(body_lines) + f"\n{END_MARKER}\n\n{user}"
        fields = {"title": f"AI Scholar · {day}", "date": day, "schema_version": SCHEMA_VERSION}
        return f"{dump_frontmatter(fields)}\n{body}"

    def render_index(
        self,
        papers: list[Paper],
        context: ExportContext,
        *,
        total_in_db: int,
        note_names: dict[str, str],
    ) -> str:
        """`00-首页.md` MOC（★ 也用标记块）。

        ★ 索引只描述**vault 状态**（有笔记的论文），不写"本次导出 N 篇"：
          否则不同选集（默认 selected / `--ids` / `--all`）会写出不同首页 ⇒ 幂等失效。
        """
        lines = ["", f"库内论文数：{total_in_db}", f"有笔记论文数：{len(papers)}"]
        lines += ["", "## 笔记索引"]
        for paper in papers[:20]:
            name = note_names.get(paper.id) or note_name(paper, max_len=self.filename_max_len)
            star = "★ " if context.watchlist_groups else ""
            lines.append(f"- {star}[[{name}]] — {paper.title[:80]}")
        lines += [""]
        fields = {"title": "AI Scholar · 首页", "schema_version": SCHEMA_VERSION}
        return (
            f"{dump_frontmatter(fields)}\n{BEGIN_MARKER}\n" + "\n".join(lines) + f"{END_MARKER}\n"
        )

    def render_topic_moc(
        self,
        existing: str,
        *,
        topic: str,
        keywords: list[str],
        entries: list[TopicEntry],
        pending: int,
        total: int,
    ) -> str:
        """`20-主题/{topic}.md` MOC（L9-B：主题维度入口）。

        ★ 三条边界：
        1. **只链到已有笔记**（`note_name is None` ⇒ 纯文本），死链比没有链接更糟；
        2. **不含生成时间** ⇒ 幂等（`--check` 会比对它，与首页同口径）；
        3. 用户区在标记块**外**，永不覆盖（与 Daily Note 同机理）。
        """
        _old_fields, old_body = parse_frontmatter(existing)
        _old_generated, old_user = split_generated(old_body)
        user = old_user.lstrip("\n")
        if USER_SECTION_TITLE not in user:
            user = f"{USER_SECTION_TITLE}\n"
        elif not user.endswith("\n"):
            user += "\n"

        lines = [
            "",
            f"命中论文：{total} 篇（有笔记 {len(entries)} 篇 · 未导出 {pending} 篇）",
            "",
        ]
        if keywords:
            lines += [f"> 关键词（watchlist 组 `{topic}`）：{'、'.join(keywords)}", ""]
        lines += ["## 有笔记"]
        if entries:
            for entry in entries:
                meta = f"`{entry.id}` · 首见 {entry.first_seen or '—'}"
                if entry.tier is not None:
                    meta += f" · Tier {entry.tier}"
                title = entry.title[:80]
                if entry.note_name:
                    lines.append(f"- [[{entry.note_name}]] — {title}（{meta}）")
                else:  # ★ 无笔记 ⇒ 不做 wikilink
                    lines.append(f"- {title}（{meta}）")
        else:
            lines.append("- 该主题下还没有笔记（收藏 / 点读后 `export` 即出现）")
        body = f"{BEGIN_MARKER}\n" + "\n".join(lines) + f"\n{END_MARKER}\n\n{user}"
        fields = {"title": f"主题 · {topic}", "topic": topic, "schema_version": SCHEMA_VERSION}
        return f"{dump_frontmatter(fields)}\n{body}"

    def render_report(
        self,
        *,
        exported: int,
        total_in_db: int,
        llm_mode: str,
        llm_usage_rows: int,
        degraded: dict[str, str],
        exported_at: datetime,
        timezone: str = DEFAULT_TZ,
    ) -> str:
        """`90-元数据/生成报告.md`（★ 导出时间只进这里，**秒级**）。

        ★ v1.4 修正：L0-C 曾把它降级为日期级以保幂等，**这是错的** ——
        报告是运行日志，本就该每次变。正确做法是**收窄幂等验收范围**
        （文献笔记 / 首页 / links.json 必须无 diff；本报告允许 diff）。
        """
        lines = [
            "",
            f"导出时间：{format_local(exported_at, timezone)}",
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
            "> ★ 本报告是**运行日志**，每次导出都会更新（含秒级时间），允许产生 diff。"
            "幂等验收只看文献笔记 / `00-首页.md` / `links.json`。",
            "",
        ]
        fields = {"title": "生成报告", "schema_version": SCHEMA_VERSION}
        return (
            f"{dump_frontmatter(fields)}\n{BEGIN_MARKER}\n" + "\n".join(lines) + f"{END_MARKER}\n"
        )

    def render_links_json(self, edges: list[LinkEdge]) -> str:
        """`90-元数据/links.json`（★ L7-7：填**真实关联**，不再是空壳）。

        ★ 幂等：排序必须确定（src → dst → type），否则每次导出顺序不同 ⇒ 假 diff。
        ★ 不读 vault、不含私有内容。
        """
        ordered = sorted(edges, key=lambda e: (e.src_id, e.dst_id, e.link_type))
        payload = {
            "schema_version": LINKS_SCHEMA_VERSION,
            "count": len(ordered),
            "links": [
                {
                    "src_id": e.src_id,
                    "dst_id": e.dst_id,
                    "link_type": e.link_type,
                    "weight": e.weight,
                    "evidence": e.evidence,
                }
                for e in ordered
            ],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    def write_links_json(self, context: ExportContext, edges: list[LinkEdge]) -> Path:
        doc = ExportDoc(
            path=Path(context.vault_dir) / "90-元数据" / "links.json",
            content=self.render_links_json(edges),
        )
        return self.write(doc)
