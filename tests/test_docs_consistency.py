"""文档一致性机械校验（阶段收尾门禁）。

★ 背景（2026-10-25 实际事故）：
每个大阶段结束都会 clear + init。若忘记新建**下一阶段**的 handoff，
下一阶段 Agent 会在没有上下文的情况下开工。当时把 L4 内容写进了 `L3-开工.md`，
文件名仍锚定 L3 —— 靠负责人提醒才发现。

⇒ 本测试把「当前阶段已标记完成 ⇒ 下一阶段 handoff 必须存在且未归档」变成 CI 门禁。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CODEBUDDY = REPO / "CODEBUDDY.md"
HANDOFF_DIR = REPO / "docs" / "handoff"

_CURRENT = re.compile(r"^##\s*★?\s*当前阶段：(.+?)——\s*(.+)$", re.MULTILINE)
_NEXT = re.compile(r"下一阶段.*?：\s*\*{0,2}(L\d+)\*{0,2}", re.MULTILINE)


def next_stage() -> str | None:
    """当前阶段已标记完成时，返回下一阶段号（如 `L4`）；否则 None。"""
    text = CODEBUDDY.read_text(encoding="utf-8")
    current = _CURRENT.search(text)
    if current is None or "已完成" not in current.group(2):
        return None  # 阶段进行中，不要求下一阶段文档
    nxt = _NEXT.search(text)
    return nxt.group(1) if nxt else None


def handoff_errors(handoff_dir: Path, stage: str) -> list[str]:
    """检查 `{stage}-开工.md` 是否存在且不是归档文件。返回错误列表（空=通过）。"""
    path = handoff_dir / f"{stage}-开工.md"
    if not path.exists():
        return [f"缺少下一阶段 handoff：{path}（clear+init 后无上下文入口）"]
    head = "\n".join(path.read_text(encoding="utf-8").splitlines()[:8])
    if "已归档" in head:
        return [f"{path} 仍是归档文件，不能作为下一阶段开工入口"]
    return []


def test_next_stage_handoff_exists() -> None:
    stage = next_stage()
    if stage is None:
        return
    errors = handoff_errors(HANDOFF_DIR, stage)
    assert not errors, "；".join(errors)


def test_ledger_mentions_next_handoff() -> None:
    stage = next_stage()
    if stage is None:
        return
    ledger = (REPO / "docs" / "文档维护日志.md").read_text(encoding="utf-8")
    assert f"{stage}-开工.md" in ledger, f"文档维护日志未登记 {stage}-开工.md"


def test_checker_detects_missing_handoff(tmp_path: Path) -> None:
    """★ 反向验证：缺文件必须能报错（否则门禁是假的）。"""
    assert handoff_errors(tmp_path, "L9")
