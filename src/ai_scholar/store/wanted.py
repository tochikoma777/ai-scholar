"""L7-6「想读队列」：从报告 / 站点点读 ⇒ 落 `selected=1` 的**真源**。

★ 为什么需要这个落点：
静态站点**没有后端**，public 仓库也无法回写 db（db 是可丢弃缓存、且不在 public 里）。
⇒ 唯一可行路径是"把意愿写进 public 真源，本地消费它"—— 与记分板
（`data/predictions/scoreboard.jsonl`）完全同构：**append-only JSONL + 幂等消费**。

语义（★ 每行只能有一个含义，别混）：
- `data/wanted.jsonl`：用户**点名想读**的论文 id（append-only，public，可人工编辑）
- 消费方：`export`（导出前）与 `collect`（采集后）把队列里的 id 置 `selected=1`
- 幂等：id 已在队列 ⇒ 不重复追加；已 `selected=1` ⇒ 不计入"本次新置位"

★ 队列里**没有**个人笔记、没有 watchlist：它是"想读清单"，公开它不破坏公私边界。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from pydantic import BaseModel

WANTED_FILENAME = "wanted.jsonl"


class WantedEntry(BaseModel):
    """一条"想读"意愿。"""

    paper_id: str
    created_at: str  # 本地日 YYYY-MM-DD
    source: str = "report"  # report | site | cli
    note: str | None = None


def wanted_path(data_dir: Path) -> Path:
    return Path(data_dir) / WANTED_FILENAME


def load_wanted(path: Path) -> list[WantedEntry]:
    """读队列（按首次出现顺序，同 id 保留第一条）。坏行跳过。"""
    if not path.exists():
        return []
    entries: list[WantedEntry] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = WantedEntry.model_validate(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            continue  # ★ 坏行跳过，但**不静默吞**：见 `load_wanted_with_errors`
        if entry.paper_id in seen:
            continue
        seen.add(entry.paper_id)
        entries.append(entry)
    return entries


def load_wanted_with_errors(path: Path) -> tuple[list[WantedEntry], list[str]]:
    """同 :func:`load_wanted`，额外返回被跳过的坏行（★ 静默失败最危险）。"""
    if not path.exists():
        return [], []
    entries: list[WantedEntry] = []
    seen: set[str] = set()
    bad: list[str] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entry = WantedEntry.model_validate(json.loads(stripped))
        except (json.JSONDecodeError, ValueError) as exc:
            bad.append(f"第 {lineno} 行：{type(exc).__name__}")
            continue
        if entry.paper_id in seen:
            continue
        seen.add(entry.paper_id)
        entries.append(entry)
    return entries, bad


def append_wanted(path: Path, entry: WantedEntry) -> bool:
    """追加一条意愿；已在队列 ⇒ 不写（幂等）。返回是否写入。"""
    if any(item.paper_id == entry.paper_id for item in load_wanted(path)):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry.model_dump(), ensure_ascii=False, sort_keys=True) + "\n")
    return True


def want_command(paper_id: str) -> str:
    """给报告 / 站点展示用的**可复制命令**（★ 这是"点读入口"的载体）。"""
    return f"uv run ai-scholar want {paper_id}"


def today_str(today: date) -> str:
    return today.isoformat()
