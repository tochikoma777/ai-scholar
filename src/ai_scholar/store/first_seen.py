"""`data/first_seen.jsonl` —— **不可逆**的首见时间真源。

★ 为什么必须有它（设计方案 §3.7.1）：`first_seen_at` 是**唯一不可再生**的数据
（"我们第一次见到它是哪天"）。db 里的 `first_seen_at` **只是缓存**，
真源是这个文件。
★ 2026-10-29 判定：**没有「一键 rebuild」命令** —— db 丢失后的恢复路径是
「重采（本文件负责去重/幂等）+ 数据源」，不要假设能从本文件单独回灌 db。

约束：
- **append-only**：已有 id **绝不覆盖**（重写即永久丢失首见日）
- **写入前先查**：同 id 已存在 → 跳过
- **幂等**：同一天跑两次 `collect`，行数不变
- 落 public：它是不可再生数据的唯一真源
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ai_scholar.config import Settings, load_settings
from ai_scholar.utils.logging import get_logger


def default_first_seen_path(settings: Settings | None = None) -> Path:
    """`data/first_seen.jsonl`（★ 不是 db）。"""
    return (settings or load_settings()).data_dir() / "first_seen.jsonl"


@dataclass(frozen=True)
class FirstSeenRecord:
    id: str
    first_seen_at: str  # YYYY-MM-DD（Asia/Shanghai 的"今天"，与 discovery 语义一致）
    source: str


class FirstSeenStore:
    """append-only 的首见时间存储。

    读：一次性把已有 id 载入内存（`known`），供"写入前先查"；
    写：只追加该文件里**没有**的 id，**已有 id 跳过**。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._known: dict[str, FirstSeenRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    # ★ 坏行不能让整条采集崩，但**必须留痕**：被跳过的 id 会被当成"没见过"，
                    #   下次 append 会再写一行（2026-10-29 审查：静默跳过 = 重复追加无人知）。
                    get_logger(__name__).warning(
                        "first_seen.jsonl 坏行（已跳过，该 id 可能被重复追加）",
                        file=str(self.path),
                        line=lineno,
                        error=type(exc).__name__,
                    )
                    continue
                pid = str(row.get("id") or "").strip()
                if pid:
                    self._known[pid] = FirstSeenRecord(
                        id=pid,
                        first_seen_at=str(row.get("first_seen_at") or ""),
                        source=str(row.get("source") or ""),
                    )

    # ── 读 ──────────────────────────────────────────────────────────────
    def get(self, paper_id: str) -> FirstSeenRecord | None:
        return self._known.get(paper_id)

    def known_ids(self) -> set[str]:
        return set(self._known)

    def __len__(self) -> int:
        return len(self._known)

    def __contains__(self, paper_id: str) -> bool:
        return paper_id in self._known

    # ── 写 ──────────────────────────────────────────────────────────────
    def append(self, paper_id: str, first_seen_at: str, source: str) -> bool:
        """追加一条。**已存在则跳过**（返回 False）—— 绝不覆盖。

        返回是否真的写入（True = 新增，False = 已有）。
        """
        if not paper_id or paper_id in self._known:
            return False
        record = FirstSeenRecord(id=paper_id, first_seen_at=first_seen_at, source=source)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "id": record.id,
                        "first_seen_at": record.first_seen_at,
                        "source": record.source,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        self._known[paper_id] = record
        return True
