"""L4 记分板：可验证推演的 JSONL 真源 + 可证伪性门禁。

★ 两类推演严格分家（设计方案 §7.1）：

| 类型 | 落点 | 进统计？ |
|---|---|---|
| 可验证判断（有量化判据 + 到期日） | `data/predictions/scoreboard.jsonl` | ✅ |
| 长期趋势洞察（不可证伪） | `data/predictions/opinions.jsonl` | ❌ **永不** |

★ **可证伪性由代码判定**：`falsifiability_errors()` 只认
`arxiv_paper_count` / `entity_mentions` / `citation_reach` 三种判据，
且必须有正阈值、正窗口、未来到期日。LLM / 用户文本无权自己宣称"不可验证"。

★ 存储是 **append-only 快照流**（public、可审计）：同一 `id` 有多行时，
**最后一行**是最新状态；验证 = 追加一行新快照，不原地改写历史。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from ai_scholar.utils.timeutil import parse_iso_date

CRITERION_TYPES = ("arxiv_paper_count", "entity_mentions", "citation_reach")
PredictionStatus = Literal["open", "hit", "miss", "insufficient"]


class Criterion(BaseModel):
    """量化判据。`target` 的语义由 `type` 决定：方向 / 实体 / 论文 id。"""

    type: str
    target: str
    threshold: int
    window_days: int


class Prediction(BaseModel):
    """一条可验证判断（★ 只有这一类能进记分板）。"""

    id: str
    claim: str
    criterion: Criterion
    created_at: str  # 本地日 YYYY-MM-DD
    due_at: str  # 本地日 YYYY-MM-DD
    status: PredictionStatus = "open"
    verified_at: str | None = None
    observed: int | None = None
    note: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    kind: Literal["verifiable"] = "verifiable"


class Opinion(BaseModel):
    """长期趋势洞察（★ 不可证伪，永不参与命中率）。"""

    id: str
    text: str
    created_at: str
    kind: Literal["opinion"] = "opinion"
    source: str = "user"


def predictions_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "predictions"


def scoreboard_path(data_dir: Path) -> Path:
    return predictions_dir(data_dir) / "scoreboard.jsonl"


def opinions_path(data_dir: Path) -> Path:
    return predictions_dir(data_dir) / "opinions.jsonl"


def prediction_id(*, created_at: str, criterion: Criterion, due_at: str) -> str:
    """确定性 id：同一天、同判据、同到期日 ⇒ 同一条（防重复生成）。"""
    raw = f"{created_at}|{criterion.type}|{criterion.target}|{criterion.threshold}|{due_at}"
    return "p-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def opinion_id(*, created_at: str, text: str) -> str:
    return "o-" + hashlib.sha1(f"{created_at}|{text}".encode()).hexdigest()[:12]


def falsifiability_errors(criterion: Criterion, *, due_at: str, today: date) -> list[str]:
    """代码级可证伪性判定。返回空列表 = 可进记分板。

    ★ 这是**硬门**：任何 LLM 输出、用户输入都必须先过这里。
    """
    errors: list[str] = []
    if criterion.type not in CRITERION_TYPES:
        errors.append(f"未知判据类型：{criterion.type!r}")
    if not criterion.target.strip():
        errors.append("判据 target 为空")
    if criterion.threshold <= 0:
        errors.append("判据 threshold 必须 > 0")
    if criterion.window_days <= 0:
        errors.append("判据 window_days 必须 > 0")
    due = parse_iso_date(due_at)
    if due is None:
        errors.append(f"到期日无法解析：{due_at!r}")
    elif due <= today:
        errors.append(f"到期日必须晚于今天：{due_at} <= {today.isoformat()}")
    return errors


def is_falsifiable(criterion: Criterion, *, due_at: str, today: date) -> bool:
    return not falsifiability_errors(criterion, due_at=due_at, today=today)


def load_latest(path: Path) -> list[Prediction]:
    """读快照流，按 `id` 折叠为最新状态（保持首次出现顺序）。坏行跳过。"""
    if not path.exists():
        return []
    order: list[str] = []
    latest: dict[str, Prediction] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            prediction = Prediction.model_validate(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            continue
        if prediction.id not in latest:
            order.append(prediction.id)
        latest[prediction.id] = prediction
    return [latest[prediction_id] for prediction_id in order]


def append_snapshot(path: Path, prediction: Prediction) -> bool:
    """追加一条快照；与最新状态完全相同则跳过（幂等）。返回是否写入。"""
    latest = {item.id: item for item in load_latest(path)}
    if latest.get(prediction.id) == prediction:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(prediction.model_dump(), ensure_ascii=False, sort_keys=True) + "\n")
    return True


def load_opinions(path: Path) -> list[Opinion]:
    if not path.exists():
        return []
    order: list[str] = []
    latest: dict[str, Opinion] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            opinion = Opinion.model_validate(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            continue
        if opinion.id not in latest:
            order.append(opinion.id)
        latest[opinion.id] = opinion
    return [latest[oid] for oid in order]


def append_opinion(path: Path, opinion: Opinion) -> bool:
    if any(item.id == opinion.id for item in load_opinions(path)):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(opinion.model_dump(), ensure_ascii=False, sort_keys=True) + "\n")
    return True
