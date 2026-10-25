"""导出编排：选范围 → 渲染 → 原子写入 → 首页 / 生成报告 / links.json。

★ 本阶段**没有任何 LLM 调用**（`llm_usage` 必然 0 行）；为什么重要 / 一句话显示 `—`。

★ 范围参数（§5.6）：vault 笔记只给 ① 已深度分析 ② 显式收藏 ③ 从报告点读。
深度分析（L3）与报告（L2）都还没做 ⇒ **本阶段只实现机制**（`--limit` / `--ids` / `--since`），
三类策略在 L3 落地。**默认绝不是全量**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import desc, or_
from sqlmodel import Session, SQLModel, col, create_engine, select

from ai_scholar.config import (
    Settings,
    load_settings,
    load_watchlist,
    match_watchlist,
    project_root,
    watchlist_paths,
)
from ai_scholar.export.base import ExportContext, ExportDoc, LinkRef
from ai_scholar.export.obsidian import ObsidianExporter, note_name
from ai_scholar.models import WatchlistConfig
from ai_scholar.pipeline.report import DailyReport, build_daily_report
from ai_scholar.store.db import default_db_path
from ai_scholar.store.models import (
    LINK_TYPE_BIBCOUPLING,
    LINK_TYPE_REFERENCES,
    LINK_TYPE_SIMILARITY,
    Paper,
    PaperLink,
    Run,
)
from ai_scholar.utils.errors import UnsafeOutputPath
from ai_scholar.utils.timeutil import day_window, today_local, utcnow


@dataclass
class ExportReport:
    exported: int = 0
    total_in_db: int = 0
    paths: list[Path] = field(default_factory=list)
    degraded: dict[str, str] = field(default_factory=dict)
    vault_dir: Path = Path("vault")
    llm_mode: str = "off"
    llm_usage_rows: int = 0
    check: bool = False
    # ★ --check：会变更的文件（文献笔记 / 首页 / links.json）；生成报告按设计不参与
    changed: list[Path] = field(default_factory=list)
    # ★ 默认只导 selected；为空时 CLI 要给明确提示，不能静默导出 0 篇
    empty_selection: bool = False


def resolve_out_dir(out: Path | str | None, settings: Settings | None = None) -> Path:
    """解析 vault 输出目录。

    规则：`--out` > `export.out_dir`；`~` 展开；相对路径按**仓库根**解析；
    最终必须落在**代码仓库之外**（双仓库防护 3）。
    """
    settings = settings or load_settings()
    raw = str(out) if out is not None else settings.export.out_dir
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = project_root() / path
    path = path.resolve()
    ensure_outside_repo(path)
    return path


def ensure_outside_repo(path: Path, *, root: Path | None = None) -> None:
    """★ 目标目录不得位于代码仓库内（含 `example_vault/`）。

    真 vault 是独立 private 仓库，与代码仓库物理隔离；写进仓库既会污染 public 提交，
    也会让 `example_vault/` 的示例语义失效。
    """
    repo = (root or project_root()).resolve()
    if path == repo or repo in path.parents:
        raise UnsafeOutputPath(
            f"vault 输出目录不得位于代码仓库内：{path}。"
            "请用 `export.out_dir` / `--out` 指向仓库外（默认 ~/projects/ai-scholar-vault）。"
        )


def load_link_refs(
    session: Session, paper_ids: list[str]
) -> tuple[
    dict[str, list[LinkRef]],
    dict[str, list[LinkRef]],
    dict[str, list[LinkRef]],
    dict[str, list[LinkRef]],
]:
    """取选集内论文的关联：`(出边, 入边, 相似, 文献耦合)`。

    - 出边 `(A → B)` = A 引用了 B：查 `src_id`
    - 入边 `(B → A)` = B 引用了 A：查 `dst_id`（**不单独存储**，反向查询得到）
    - 对称边（`similarity` / `bibcoupling`）：只存一条（a<b），这里双向展开
    """
    out_map: dict[str, list[LinkRef]] = {pid: [] for pid in paper_ids}
    in_map: dict[str, list[LinkRef]] = {pid: [] for pid in paper_ids}
    similar_map: dict[str, list[LinkRef]] = {pid: [] for pid in paper_ids}
    coupled_map: dict[str, list[LinkRef]] = {pid: [] for pid in paper_ids}
    if not paper_ids:
        return out_map, in_map, similar_map, coupled_map

    ids = set(paper_ids)
    out_rows = list(
        session.exec(
            select(PaperLink).where(
                col(PaperLink.src_id).in_(paper_ids),
                PaperLink.link_type == LINK_TYPE_REFERENCES,
            )
        ).all()
    )
    in_rows = list(
        session.exec(
            select(PaperLink).where(
                col(PaperLink.dst_id).in_(paper_ids),
                PaperLink.link_type == LINK_TYPE_REFERENCES,
            )
        ).all()
    )
    sym_rows = list(
        session.exec(
            select(PaperLink).where(
                col(PaperLink.link_type).in_((LINK_TYPE_BIBCOUPLING, LINK_TYPE_SIMILARITY)),
                or_(
                    col(PaperLink.src_id).in_(paper_ids),
                    col(PaperLink.dst_id).in_(paper_ids),
                ),
            )
        ).all()
    )

    target_ids = {r.dst_id for r in out_rows} | {r.src_id for r in in_rows}
    target_ids |= {r.dst_id for r in sym_rows} | {r.src_id for r in sym_rows}
    papers = {
        p.id: p for p in session.exec(select(Paper).where(col(Paper.id).in_(target_ids))).all()
    }

    def to_ref(pid: str, link: PaperLink) -> LinkRef:
        target = papers.get(pid)
        return LinkRef(
            paper_id=pid,
            title=(target.title if target else pid),
            url=(target.url if target else None),
            link_type=link.link_type,
            weight=link.weight,
            evidence=link.evidence,
            interpretation=link.interpretation,
            published_at=(target.published_at if target else None),
        )

    for row in out_rows:
        if row.src_id in out_map:
            out_map[row.src_id].append(to_ref(row.dst_id, row))
    for row in in_rows:
        if row.dst_id in in_map:
            in_map[row.dst_id].append(to_ref(row.src_id, row))
    for row in sym_rows:
        bucket = similar_map if row.link_type == LINK_TYPE_SIMILARITY else coupled_map
        if row.src_id in ids:
            bucket[row.src_id].append(to_ref(row.dst_id, row))
        if row.dst_id in ids:
            bucket[row.dst_id].append(to_ref(row.src_id, row))
    return out_map, in_map, similar_map, coupled_map


def _file_matches(path: Path, content: str) -> bool:
    """文件内容是否与待写入内容**逐字节**一致（不存在即视为不一致）。"""
    try:
        return path.read_bytes() == content.encode("utf-8")
    except (FileNotFoundError, IsADirectoryError):
        return False


def select_papers(
    session: Session,
    *,
    limit: int,
    ids: list[str] | None = None,
    since: date | None = None,
    all_papers: bool = False,
) -> list[Paper]:
    """按范围选出要导出的论文。

    ★ 默认**只导 `selected=1`**（§5.6 v1.7）：否则每天 export 都会造 20 篇新笔记
      （≈7,300 篇/年，是 300–500 篇目标的 15 倍）。`--all` 才按 limit 导最新（demo/调试）。
    排序：`published_at` desc，`hf_score` 兜底（★ 与入库顺序无关）。
    """
    if ids:
        wanted = set(ids)
        rows = session.exec(select(Paper)).all()
        picked = [p for p in rows if p.id in wanted]
        picked.sort(key=lambda p: (p.published_at or "", p.hf_score or 0), reverse=True)
        return picked[:limit] if limit > 0 else picked

    statement = select(Paper)
    if not all_papers:
        statement = statement.where(col(Paper.selected).is_(True))
    if since is not None:
        statement = statement.where(Paper.first_seen_at >= since.isoformat())
    statement = statement.order_by(desc(col(Paper.published_at)), desc(col(Paper.hf_score)))
    rows = list(session.exec(statement).all())
    return rows[:limit] if limit > 0 else rows


def _watchlist_degraded() -> dict[str, str]:
    """★ watchlist.local.yaml 缺失是 warning，但必须显式暴露在报告里。"""
    _default_path, local_path = watchlist_paths()
    if not local_path.exists():
        return {"watchlist.local.yaml": "缺失（当前用仓库里的通用示例；`ai-scholar init` 可复制）"}
    return {}


def _hf_degraded(session: Session) -> dict[str, str]:
    """HF 是否不可达：从**最近一次采集**的 `runs.degraded_json` 读（不重新发请求）。"""
    run = session.exec(select(Run).order_by(desc(col(Run.started_at)))).first()
    if run is None:
        return {"HF Daily Papers": "未知（尚无采集记录）"}
    if not run.degraded_json:
        return {}
    import json

    try:
        degraded = json.loads(run.degraded_json)
    except json.JSONDecodeError:
        return {"HF Daily Papers": "degraded_json 解析失败"}
    if "hf_daily" in degraded:
        return {"HF Daily Papers": f"不可达（{degraded['hf_daily']}）"}
    return {}


DAILY_MAX_ITEMS = 20


def _daily_note_link(
    paper: Paper, *, exporter: ObsidianExporter, context: ExportContext, max_len: int
) -> str:
    """Daily Note 条目行：有笔记 → `[[双链]]`；无笔记 → 文本 + 外链（不建空笔记）。"""
    name = None
    if paper.note_path:
        name = Path(paper.note_path).stem
    elif exporter.note_path(paper, context).exists():
        name = note_name(paper, max_len=max_len)
    if name:
        return f"- [[{name}]]"
    return f"- ★ {paper.title}（无笔记）— {paper.url or '（无外链）'}"


def _daily_note_lines(
    session: Session,
    *,
    day: date,
    daily: DailyReport,
    watchlist: WatchlistConfig,
    exporter: ObsidianExporter,
    context: ExportContext,
    max_len: int,
) -> list[str]:
    """生成区内容（§6.6）：今日新增 / 补齐引用 / 深度分析 / 新定级 / ★命中 / 运行状态。"""
    day_str = day.isoformat()
    papers = list(session.exec(select(Paper)).all())
    by_id = {p.id: p for p in papers}

    def line_for(paper: Paper, *, groups: list[str] | None = None, extra: str = "") -> str:
        line = _daily_note_link(paper, exporter=exporter, context=context, max_len=max_len)
        if groups:
            line += f" ★ {', '.join(groups)}"
        if extra:
            line += f"（{extra}）"
        return line

    lines = [f"# {day_str}", ""]

    new_papers = sorted(
        [p for p in papers if p.first_seen_at == day_str],
        key=lambda p: (p.published_at or "", p.hf_score or 0),
        reverse=True,
    )
    lines += [f"## 今日新增（{len(new_papers)} 篇）"]
    if new_papers:
        for paper in new_papers[:DAILY_MAX_ITEMS]:
            hit = match_watchlist(watchlist, paper.title, paper.abstract or "")
            lines.append(line_for(paper, groups=hit.groups if hit.matched else None))
        if len(new_papers) > DAILY_MAX_ITEMS:
            lines.append(f"（还有 {len(new_papers) - DAILY_MAX_ITEMS} 篇）")
    else:
        lines.append("（今日无新增）")
    lines.append("")

    start, _ = day_window(day)
    start_iso = start.isoformat(timespec="seconds")
    enriched = [p for p in papers if (p.enrich_last_at or "") >= start_iso]
    lines += [f"## 今日补齐引用（{len(enriched)} 篇）"]
    if enriched:
        for paper in enriched[:DAILY_MAX_ITEMS]:
            lines.append(line_for(paper))
    else:
        lines.append("（无）")
    lines.append("")

    for heading, hits, with_tier in (
        ("今日深度分析", daily.deep_analyzed, False),
        ("今日新定级", daily.new_graded, True),
        ("★命中 Top10", daily.watchlist_hits, False),
    ):
        lines += [f"## {heading}（{len(hits)} 篇）"]
        if hits:
            for entry in hits:
                target = by_id.get(entry.id)
                if target is None:
                    lines.append(f"- {entry.title}")
                    continue
                extra = f"Tier {target.tier}" if with_tier and target.tier else ""
                lines.append(line_for(target, groups=entry.groups, extra=extra))
        else:
            lines.append("（无）")
        lines.append("")

    lines += ["## 运行状态"]
    if daily.degraded:
        for key, reason in sorted(daily.degraded.items()):
            lines.append(f"- {key}：{reason}")
    else:
        lines.append("（无降级）")
    lines.append("")

    lines += ["## 今日推演", "- （L4）"]
    return lines


def run_export(
    *,
    limit: int | None = None,
    ids: list[str] | None = None,
    since: date | None = None,
    out: Path | None = None,
    settings: Settings | None = None,
    db_path: Path | None = None,
    llm_mode: str | None = None,
    check: bool = False,
    exported_at: datetime | None = None,
    all_papers: bool = False,
    today: date | None = None,
) -> ExportReport:
    """跑一次导出（同步：本阶段无网络调用）。

    ★ `check=True` 只做**逐字节比对**、不落盘：列出会变更的文件，供幂等验收。
      比对范围 = 文献笔记 + `00-首页.md` + `links.json`；
      `生成报告.md` 是运行日志，按设计允许 diff，不参与比对。
    ★ `all_papers=True`：非 check 时按 `--limit` 导最新（demo/调试）；
      check 时校验**全量笔记**（所有 `note_path` 非空的论文），补 L1-0 的局限。
    """
    settings = settings or load_settings()
    report = ExportReport()
    report.check = check
    report.llm_mode = llm_mode or settings.effective_mode().value

    # ★ 先校验输出路径：落在代码仓库内直接拒绝（防 example_vault / 手滑）
    vault_dir = resolve_out_dir(out, settings)
    report.vault_dir = vault_dir

    db_file = Path(db_path) if db_path else default_db_path(settings)
    engine = create_engine(f"sqlite:///{db_file}", echo=False)
    SQLModel.metadata.create_all(engine)

    effective_limit = limit if limit is not None else settings.export.default_limit
    exporter = ObsidianExporter(
        filename_max_len=settings.export.filename_max_len,
        links_max_per_section=settings.export.links_max_per_section,
    )

    docs: list[ExportDoc] = []
    with Session(engine) as session:
        if check and all_papers:
            # ★ --check --all：校验**磁盘上真实存在的全部笔记**（不只本次选集）。
            #   不依赖 note_path 是否回填过：按论文算一遍目标路径，文件存在就校验。
            probe = ExportContext(vault_dir=vault_dir)
            papers = [
                p
                for p in session.exec(select(Paper)).all()
                if p.note_path or exporter.note_path(p, probe).exists()
            ]
            papers.sort(key=lambda p: (p.published_at or "", p.hf_score or 0), reverse=True)
        else:
            papers = select_papers(
                session,
                limit=effective_limit,
                ids=ids,
                since=since,
                all_papers=all_papers,
            )
        report.total_in_db = len(session.exec(select(Paper)).all())
        report.empty_selection = not papers
        if report.empty_selection and not check:
            # ★ 无选中论文：不写任何文件（避免造出空首页 / 空报告），由 CLI 给提示
            return report

        watchlist = load_watchlist()
        # ★ 双轨渲染索引：本次导出的论文才有 vault 笔记；其余目标只给文本 + 外链
        note_index = {p.id: note_name(p, max_len=settings.export.filename_max_len) for p in papers}

        # ★ 关联边（出边 / 入边）→ 渲染用 LinkRef
        out_map, in_map, similar_map, coupled_map = load_link_refs(session, [p.id for p in papers])
        target_ids = {
            ref.paper_id
            for refs in [
                *out_map.values(),
                *in_map.values(),
                *similar_map.values(),
                *coupled_map.values(),
            ]
            for ref in refs
            if ref.paper_id not in note_index
        }
        if target_ids:
            # ★ 目标有没有笔记，看 db 里上次导出回填的 note_path（不扫目录）
            for target in session.exec(select(Paper).where(col(Paper.id).in_(target_ids))).all():
                if target.note_path:
                    note_index[target.id] = Path(target.note_path).stem

        report.degraded.update(_watchlist_degraded())
        report.degraded.update(_hf_degraded(session))

        for paper in papers:
            hit = match_watchlist(watchlist, paper.title, paper.abstract or "")
            context = ExportContext(
                llm_mode=report.llm_mode,
                vault_dir=vault_dir,
                note_index=note_index,
                out_links=out_map.get(paper.id, []),
                in_links=in_map.get(paper.id, []),
                similar=similar_map.get(paper.id, []),
                coupled=coupled_map.get(paper.id, []),
                watchlist_groups=hit.groups if hit.matched else [],
                degraded=report.degraded,
            )
            content = exporter.render(paper, context)
            path = exporter.note_path(paper, context)
            docs.append(ExportDoc(path=path, content=content))
            if not check:
                # ★ 回填 vault 笔记相对路径（NULL = 无笔记）→ 下次导出的双轨渲染据此判断
                rel = str(path.relative_to(vault_dir))
                if paper.note_path != rel:
                    paper.note_path = rel

        # links.json（本阶段空结构）
        docs.append(
            ExportDoc(
                path=vault_dir / "90-元数据" / "links.json",
                content=exporter.render_links_json([]),
            )
        )

        if not check:
            session.commit()  # ★ note_path 回填要落库（双轨渲染的输入）

        # 00-首页.md
        # ★ 索引描述 **vault 状态**（磁盘上已有笔记 ∪ 本次要写的笔记），与本次选集无关
        probe = ExportContext(vault_dir=vault_dir)
        selected_ids = {p.id for p in papers}
        index_papers = [
            p
            for p in session.exec(select(Paper)).all()
            if p.id in selected_ids or p.note_path or exporter.note_path(p, probe).exists()
        ]
        index_papers.sort(key=lambda p: (p.published_at or "", p.hf_score or 0), reverse=True)
        index_names = {
            p.id: note_name(p, max_len=settings.export.filename_max_len) for p in index_papers
        }
        index_context = ExportContext(llm_mode=report.llm_mode, vault_dir=vault_dir)
        index_text = exporter.render_index(
            index_papers,
            index_context,
            total_in_db=report.total_in_db,
            note_names=index_names,
        )
        docs.append(ExportDoc(path=vault_dir / "00-首页.md", content=index_text))

        # 40-日志/YYYY-MM-DD.md（L2-C Daily Note：本地 export 产物，§6.6）
        day = today or today_local()
        daily = build_daily_report(session, today=day, watchlist=watchlist)
        note_lines = _daily_note_lines(
            session,
            day=day,
            daily=daily,
            watchlist=watchlist,
            exporter=exporter,
            context=ExportContext(vault_dir=vault_dir),
            max_len=settings.export.filename_max_len,
        )
        note_file = vault_dir / "40-日志" / f"{day.isoformat()}.md"
        existing = note_file.read_text(encoding="utf-8") if note_file.exists() else ""
        docs.append(
            ExportDoc(
                path=note_file,
                content=exporter.render_daily_note(
                    existing, day=day.isoformat(), body_lines=note_lines
                ),
            )
        )

        # 90-元数据/生成报告.md（★ 导出时间只进这里，秒级；作为运行日志允许 diff）
        report_text = exporter.render_report(
            exported=len(papers),
            total_in_db=report.total_in_db,
            llm_mode=report.llm_mode,
            llm_usage_rows=0,  # ★ 本阶段零 LLM 调用
            degraded=report.degraded,
            exported_at=exported_at or utcnow(),
            timezone=settings.app.timezone,
        )

    report.exported = len(papers)
    report.changed = [doc.path for doc in docs if not _file_matches(doc.path, doc.content)]
    if check:
        return report

    for doc in docs:
        exporter.write(doc)
        report.paths.append(doc.path)
    report_doc = ExportDoc(
        path=vault_dir / "90-元数据" / "生成报告.md",
        content=report_text,
    )
    exporter.write(report_doc)
    report.paths.append(report_doc.path)
    return report
