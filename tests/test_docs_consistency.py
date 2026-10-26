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
# ★ `path.py:NNN symbol` —— CODEBUDDY 里的行号引用必须有机器校验（2026-10-25 已漂 2 行）
_ANCHOR = re.compile(r"`([A-Za-z0-9_./]+\.py):(\d+)\s+([A-Za-z_][A-Za-z0-9_]*)`")


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


def anchor_errors(text: str) -> list[str]:
    """CODEBUDDY 里 `path.py:NNN symbol` 形式的行号引用是否仍然对得上。

    ★ 为什么需要：文档写「关键路径在某某行」，改代码后行号会漂 —— 2026-10-25 实测漂了 2 行
    （`collect.py:259` 实际是 261）。人工核对不可靠，交给机器。
    """
    refs = _ANCHOR.findall(text)
    if not refs:
        return ["CODEBUDDY.md 里没有任何 `path.py:行号 符号` 引用 —— 锚定器可能已失效"]
    errors: list[str] = []
    for path_str, lineno, symbol in refs:
        rel = Path(path_str)
        if path_str.startswith(("src/", "tests/")):
            target = REPO / rel
        else:
            target = REPO / "src" / "ai_scholar" / rel
        if not target.exists():
            errors.append(f"{path_str}:{lineno} 文件不存在")
            continue
        lines = target.read_text(encoding="utf-8").splitlines()
        n = int(lineno)
        if n < 1 or n > len(lines):
            errors.append(f"{path_str}:{lineno} 超出文件行数（{len(lines)} 行）")
            continue
        # 容差：装饰器会让 `def` 落在行号之后几行
        window = lines[n - 1 : n + 4]
        pattern = re.compile(
            rf"^\s*(?:async\s+)?def\s+{re.escape(symbol)}\b|^\s*{re.escape(symbol)}\s*[:=]"
        )
        if not any(pattern.search(line) for line in window):
            errors.append(f"{path_str}:{lineno} 附近找不到 `{symbol}`（行号已漂移）")
    return errors


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


def test_codebuddy_line_anchors() -> None:
    """CODEBUDDY 里的行号引用必须仍然指到它说的那个函数。"""
    errors = anchor_errors(CODEBUDDY.read_text(encoding="utf-8"))
    assert not errors, "；".join(errors)


def test_checker_detects_missing_handoff(tmp_path: Path) -> None:
    """★ 反向验证：缺文件必须能报错（否则门禁是假的）。"""
    assert handoff_errors(tmp_path, "L9")


def test_anchor_checker_detects_drift() -> None:
    """★ 反向验证：行号漂了必须报错、对的必须放行（否则锚定器是假的）。"""
    target = REPO / "src" / "ai_scholar" / "pipeline" / "collect.py"
    lines = target.read_text(encoding="utf-8").splitlines()
    good = next(i for i, line in enumerate(lines, 1) if line.startswith("async def run_collect"))
    assert anchor_errors(f"**collect** — `pipeline/collect.py:{good - 5} run_collect`")
    assert not anchor_errors(f"**collect** — `pipeline/collect.py:{good} run_collect`")


_CURRENT_HANDOFF = re.compile(r"当前\s*`(L\d+)-开工\.md`")


def current_handoff_pointer(text: str, handoff_dir: Path) -> list[str]:
    """CODEBUDDY 里"当前 `Lx-开工.md`"必须指向**阶段号最大且未归档**的那份。

    ★ 背景（2026-10-26 已两次踩中）：L7 开工后仍写着 L5、L8 完成后仍写着 L7 ——
      clear+init 后的 Agent 会照着**已归档**的范围开工（G4 交接文档要能盲测）。
    """
    match = _CURRENT_HANDOFF.search(text)
    if match is None:
        return ["CODEBUDDY.md 没有『当前 `Lx-开工.md`』指针 —— 交接入口缺失"]
    stage = match.group(1)
    candidates = sorted(
        (p for p in handoff_dir.glob("L*-开工.md") if re.fullmatch(r"L\d+-开工\.md", p.name)),
        key=lambda p: int(re.match(r"L(\d+)", p.name).group(1)),  # type: ignore[union-attr]
    )
    if not candidates:
        return ["docs/handoff/ 下没有任何 L*-开工.md"]
    latest = candidates[-1].stem.split("-")[0]
    errors: list[str] = []
    if stage != latest:
        errors.append(
            f"CODEBUDDY 指向 {stage}，但最新阶段是 {latest}（开工入口指错 = 按归档范围开工）"
        )
    target = handoff_dir / f"{stage}-开工.md"
    if not target.exists():
        errors.append(f"{target.name} 不存在")
    else:
        head = "\n".join(target.read_text(encoding="utf-8").splitlines()[:8])
        if "已归档" in head:
            errors.append(f"{target.name} 已归档，不能作为开工入口")
    return errors


def test_current_handoff_pointer_is_latest() -> None:
    errors = current_handoff_pointer(CODEBUDDY.read_text(encoding="utf-8"), HANDOFF_DIR)
    assert not errors, "；".join(errors)


def previous_handoff_errors(handoff_dir: Path) -> list[str]:
    """除**阶段号最大**的那份，其余 handoff 头部必须含「已归档」。

    ★ 背景（2026-10-26 审计发现）：`L2-开工.md` / `L6-开工.md` 阶段早已完成，
    但头部既无「已归档」也不指向下一阶段，文件名却仍叫「开工」——
    按目录浏览的人会把它当成入口。原门禁只校验「CODEBUDDY 指针」与「下一阶段文件」，
    历史文件没人管。
    """
    candidates = sorted(
        (p for p in handoff_dir.glob("L*-开工.md") if re.fullmatch(r"L\d+-开工\.md", p.name)),
        key=lambda p: int(re.match(r"L(\d+)", p.name).group(1)),  # type: ignore[union-attr]
    )
    if len(candidates) < 2:
        return []
    errors: list[str] = []
    for path in candidates[:-1]:
        head = "\n".join(path.read_text(encoding="utf-8").splitlines()[:8])
        if "已归档" not in head:
            errors.append(f"{path.name} 已完成但头部无「已归档」，会被误当作开工入口")
    return errors


def test_all_previous_handoffs_archived() -> None:
    assert not previous_handoff_errors(HANDOFF_DIR), "；".join(previous_handoff_errors(HANDOFF_DIR))


def test_archived_checker_detects_unmarked(tmp_path: Path) -> None:
    """★ 反向验证：历史 handoff 没标归档必须报错（否则门禁是假的）。"""
    (tmp_path / "L2-开工.md").write_text("# L2 交接 · 开工\n\n正文", encoding="utf-8")
    (tmp_path / "L9-开工.md").write_text("# L9\n正文", encoding="utf-8")
    assert previous_handoff_errors(tmp_path)
    (tmp_path / "L2-开工.md").write_text("# L2 交接 · 已归档\n", encoding="utf-8")
    assert not previous_handoff_errors(tmp_path)


def test_pointer_checker_detects_stale_pointer(tmp_path: Path) -> None:
    """★ 反向验证：指向上一个阶段必须报错（否则门禁是假的）。"""
    (tmp_path / "L8-开工.md").write_text("# L8\n> 已归档", encoding="utf-8")
    (tmp_path / "L9-开工.md").write_text("# L9\n正文", encoding="utf-8")
    assert current_handoff_pointer("当前 `L8-开工.md`", tmp_path)
    assert not current_handoff_pointer("当前 `L9-开工.md`", tmp_path)
