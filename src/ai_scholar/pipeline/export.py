"""导出编排：选范围 → 渲染 → 原子写入 → 首页 / 生成报告 / links.json。

★ 本阶段**没有任何 LLM 调用**（`llm_usage` 必然 0 行）；为什么重要 / 一句话显示 `—`。

★ 范围参数（§5.6）：vault 笔记只给 ① 已深度分析 ② 显式收藏 ③ 从报告点读。
深度分析（L3）与报告（L2）都还没做 ⇒ **本阶段只实现机制**（`--limit` / `--ids` / `--since`），
三类策略在 L3 落地。**默认绝不是全量**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from sqlalchemy import desc
from sqlmodel import Session, SQLModel, col, create_engine, select

from ai_scholar.config import (
    Settings,
    load_settings,
    load_watchlist,
    match_watchlist,
    watchlist_paths,
)
from ai_scholar.export.base import ExportContext, ExportDoc
from ai_scholar.export.obsidian import ObsidianExporter, note_name
from ai_scholar.store.db import default_db_path
from ai_scholar.store.models import Paper, Run
from ai_scholar.utils.timeutil import today_local


@dataclass
class ExportReport:
    exported: int = 0
    total_in_db: int = 0
    paths: list[Path] = field(default_factory=list)
    degraded: dict[str, str] = field(default_factory=dict)
    vault_dir: Path = Path("vault")
    llm_mode: str = "off"
    llm_usage_rows: int = 0


def select_papers(
    session: Session,
    *,
    limit: int,
    ids: list[str] | None = None,
    since: date | None = None,
) -> list[Paper]:
    """按范围选出要导出的论文。

    排序：`published_at` desc，`hf_score` 兜底（★ 与入库顺序无关）。
    """
    if ids:
        wanted = set(ids)
        rows = session.exec(select(Paper)).all()
        picked = [p for p in rows if p.id in wanted]
        picked.sort(key=lambda p: (p.published_at or "", p.hf_score or 0), reverse=True)
        return picked[:limit] if limit > 0 else picked

    statement = select(Paper)
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


def run_export(
    *,
    limit: int | None = None,
    ids: list[str] | None = None,
    since: date | None = None,
    out: Path | None = None,
    settings: Settings | None = None,
    db_path: Path | None = None,
    llm_mode: str | None = None,
) -> ExportReport:
    """跑一次导出（同步：本阶段无网络调用）。"""
    settings = settings or load_settings()
    report = ExportReport()
    report.llm_mode = llm_mode or settings.effective_mode().value

    db_file = Path(db_path) if db_path else default_db_path(settings)
    engine = create_engine(f"sqlite:///{db_file}", echo=False)
    SQLModel.metadata.create_all(engine)

    vault_dir = Path(out) if out else Path(settings.export.vault_dir)
    if not vault_dir.is_absolute():
        vault_dir = settings.data_dir().parent / vault_dir
    report.vault_dir = vault_dir

    effective_limit = limit if limit is not None else settings.export.default_limit
    exporter = ObsidianExporter(filename_max_len=settings.export.filename_max_len)

    with Session(engine) as session:
        papers = select_papers(session, limit=effective_limit, ids=ids, since=since)
        report.total_in_db = len(session.exec(select(Paper)).all())

        watchlist = load_watchlist()
        # ★ 双轨渲染索引：本次导出的论文才有 vault 笔记；其余目标只给文本 + 外链
        note_index = {p.id: note_name(p, max_len=settings.export.filename_max_len) for p in papers}

        report.degraded.update(_watchlist_degraded())
        report.degraded.update(_hf_degraded(session))

        for paper in papers:
            hit = match_watchlist(watchlist, paper.title, paper.abstract or "")
            context = ExportContext(
                llm_mode=report.llm_mode,
                vault_dir=vault_dir,
                note_index=note_index,
                watchlist_groups=hit.groups if hit.matched else [],
                degraded=report.degraded,
            )
            content = exporter.render(paper, context)
            path = exporter.note_path(paper, context)
            exporter.write(ExportDoc(path=path, content=content))
            report.paths.append(path)

        # links.json（本阶段空结构）
        exporter.write_links_json(ExportContext(vault_dir=vault_dir), [])

        # 00-首页.md
        index_context = ExportContext(llm_mode=report.llm_mode, vault_dir=vault_dir)
        index_text = exporter.render_index(
            papers, index_context, total_in_db=report.total_in_db, note_names=note_index
        )
        exporter.write(ExportDoc(path=vault_dir / "00-首页.md", content=index_text))

        # 90-元数据/生成报告.md（★ 导出时间只进这里）
        report_text = exporter.render_report(
            exported=len(papers),
            total_in_db=report.total_in_db,
            llm_mode=report.llm_mode,
            llm_usage_rows=0,  # ★ 本阶段零 LLM 调用
            degraded=report.degraded,
            export_day=today_local(),
        )
        exporter.write(ExportDoc(path=vault_dir / "90-元数据" / "生成报告.md", content=report_text))

    report.exported = len(papers)
    return report
