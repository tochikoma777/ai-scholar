"""工作流接线机械门禁（审查报告 2026-11-10 · R1）。

★ 背景：`tests/test_docs_consistency.py` 只守文档指针 / 行号 / 归档，
  完全不碰 `.github/workflows/`。2026-11-01 / 11-02 两次 recap schedule failure
  的根因都在工作流里：
    1. `AI_SCHOLAR_MODE: off` 裸写 ⇒ YAML 1.1 解析成布尔 False ⇒ ValidationError；
    2. `git add data/reports/weekly data/reports/monthly` 在只跑一种周期时
       对不存在的目录 pathspec fatal。
  直到 2026-11-05 才由 `3e1f617` 修复。本文件把这两类事故 + 命令接线 + 并发组
  变成 CI 红绿（E7「能力已实现≠已接线」的机械守卫）。

★ 用纯文本解析而非 yaml.safe_load：GitHub Actions 的 `on:` 会被 YAML 1.1
  解析成布尔 True（正是本文件要防的那类坑），且 `${{ }}` 表达式不适合强解析。

★ 每个 checker 都有反向验证（改坏必须报错），否则门禁是假的 ——
  与 `test_docs_consistency.py` 同规格。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"
CLI = REPO / "src" / "ai_scholar" / "cli.py"

_WORKFLOW_FILES = ("daily.yml", "recap.yml", "ci.yml")

# 工作流里 `uv run ai-scholar <cmd>` 的子命令（--flag / 位置参数不属于命令名）
_CMD = re.compile(r"uv run ai-scholar\s+([a-z][a-z0-9_-]*)")
# cli.py 里 Typer 注册的命令：`@app.command()` 紧跟 `def <name>(`
_REGISTERED = re.compile(r"@app\.command\([^)]*\)\s*\ndef\s+(\w+)\(")
# YAML 1.1 会把裸 off/on/yes/no/true/false 解析成布尔（config/default.yaml 已踩）
_MODE = re.compile(r"^\s*AI_SCHOLAR_MODE:\s*(.+?)\s*$", re.MULTILINE)
_YAML11_BOOL = re.compile(r"^(off|on|yes|no|true|false|null|~)$", re.IGNORECASE)
_DIR_GUARD = re.compile(r"if\s*\[\s*-d\s")
_UNGUARDED_RECAP_ADD = re.compile(
    r"^\s*git add\s+[^\n]*data/reports/weekly[^\n]*data/reports/monthly", re.MULTILINE
)
_GROUP = re.compile(r"^\s*group:\s*(\S+)\s*$", re.MULTILINE)
_COMMIT_STEP = re.compile(
    r"- name: Commit and push[ \t]*\n[ \t]*run: \|[ \t]*\n"
    r"(?P<body>(?:[ \t]+.*\n|[ \t]*\n)+)"
)
# ★ 2026-11-11：DeepSeek 高峰守卫必须在 LLM 步骤之前（防排队延迟把调用拖进高峰）
_GUARD = re.compile(
    r"- name: Peak-hour guard[ \t]*\n[ \t]*run: uv run python -m ai_scholar\.utils\.peak_hours"
)
_LLM_STEPS = ("- name: Enhance", "- name: Grade", "- name: Analyze")


# ── 纯 checker（供正向断言与反向验证共用）──────────────────────────────


def registered_commands(cli_text: str) -> set[str]:
    return set(_REGISTERED.findall(cli_text))


def workflow_commands(text: str) -> set[str]:
    return set(_CMD.findall(text))


def command_errors(workflow_text: str, registered: set[str]) -> list[str]:
    missing = sorted(workflow_commands(workflow_text) - registered)
    return [f"workflow 调用了未注册的 ai-scholar 命令：{missing}"] if missing else []


def mode_errors(text: str) -> list[str]:
    """`AI_SCHOLAR_MODE` 值必须带引号 —— 裸 off/on/yes/no 会被 YAML 1.1 吃成布尔。"""
    return [
        f"AI_SCHOLAR_MODE 必须带引号（YAML 1.1 会把 {raw.strip()} 解析成布尔）：{raw.strip()}"
        for raw in _MODE.findall(text.strip())
        if _YAML11_BOOL.match(raw.strip())
    ]


def commit_step(text: str) -> str:
    """取 `Commit and push` 的 `run: |` 块；解析不到返回空串（由调用方断言）。"""
    match = _COMMIT_STEP.search(text)
    return match.group("body") if match else ""


def recap_add_errors(commit_text: str) -> list[str]:
    """recap 一次只生成一个周期 ⇒ 另一目录不存在；裸 git add 会 pathspec fatal。

    `daily.yml` 的多路径 add 不在本检查范围内：它 add 的是**固定存在的真源**
    （first_seen / signals / reports / predictions / analyses），不需要 -d 守卫。
    """
    errors: list[str] = []
    if "git add" not in commit_text:
        return ["recap 的 Commit and push 步骤没有 git add ⇒ 派生产物不会被提交"]
    if _UNGUARDED_RECAP_ADD.search(commit_text):
        errors.append("recap 把 weekly/ 与 monthly/ 写进同一条裸 git add")
    if not _DIR_GUARD.search(commit_text):
        errors.append("recap 的 git add 缺少 `if [ -d ... ]` 目录存在守卫")
    return errors


def group_errors(groups: dict[str, str | None]) -> list[str]:
    """daily / recap 都会 push 到 main，必须共用 concurrency 组（G5：否则互相覆盖真源）。"""
    pushers = {name: groups.get(name) for name in ("daily.yml", "recap.yml")}
    missing = [name for name, value in pushers.items() if value is None]
    if missing:
        return [f"缺少 concurrency.group：{missing}"]
    distinct = set(pushers.values())
    if len(distinct) != 1:
        return [f"daily / recap 的 concurrency 组不一致，会互相覆盖 public 真源：{pushers}"]
    return []


def guard_errors(text: str) -> list[str]:
    """daily.yml 必须有高峰守卫，且位于 Enhance / Grade / Analyze 之前。"""
    match = _GUARD.search(text)
    if match is None:
        return ["daily.yml 缺少 Peak-hour guard（LLM 调用可能落入 DeepSeek 高峰时段）"]
    errors: list[str] = []
    for step in _LLM_STEPS:
        position = text.find(step)
        if position != -1 and position < match.start():
            errors.append(f"{step} 在 Peak-hour guard 之前，守卫无效")
    return errors


def _read_workflows() -> dict[str, str]:
    return {name: (WORKFLOWS / name).read_text(encoding="utf-8") for name in _WORKFLOW_FILES}


# ── 正向断言 ────────────────────────────────────────────────────────────


def test_workflow_cli_commands_are_registered() -> None:
    registered = registered_commands(CLI.read_text(encoding="utf-8"))
    assert registered, "cli.py 没解析出任何 @app.command（正则可能失效）"
    workflows = _read_workflows()
    for name, text in workflows.items():
        assert not command_errors(text, registered), name
    # ★ 防止「解析出的命令集为空 ⇒ 差集恒空」的恒真门禁
    found: set[str] = set()
    for text in workflows.values():
        found |= workflow_commands(text)
    assert len(found) >= 10, f"工作流里只解析出 {len(found)} 个命令，正则可能失效"


def test_ai_scholar_mode_values_are_quoted() -> None:
    for name, text in _read_workflows().items():
        assert not mode_errors(text), name


def test_recap_commit_step_guards_optional_directories() -> None:
    text = (WORKFLOWS / "recap.yml").read_text(encoding="utf-8")
    step = commit_step(text)
    assert step, "没解析出 recap.yml 的 Commit and push 块（步骤改名或结构变了）"
    assert not recap_add_errors(step)


def test_push_workflows_share_concurrency_group() -> None:
    groups = {
        name: (match.group(1) if (match := _GROUP.search(text)) else None)
        for name, text in _read_workflows().items()
    }
    assert not group_errors(groups), groups


def test_daily_has_peak_hour_guard_before_llm_steps() -> None:
    """★ 2026-11-11：DeepSeek 分时定价 —— 高峰前必须降级 off（见 utils/peak_hours.py）。"""
    text = (WORKFLOWS / "daily.yml").read_text(encoding="utf-8")
    assert not guard_errors(text), guard_errors(text)


def test_peak_guard_constants_match_official_definition() -> None:
    """窗口常量必须与官方一致：UTC 01:00–04:00 / 06:00–10:00，前后各 15 分钟余量。"""
    from ai_scholar.utils.peak_hours import MARGIN_MIN, PEAK_WINDOWS_UTC

    assert PEAK_WINDOWS_UTC == ((60, 240), (360, 600))
    assert MARGIN_MIN == 15


# ── 反向验证（门禁不能是假的）──────────────────────────────────────────


def test_checker_detects_unregistered_command() -> None:
    assert command_errors("uv run ai-scholar frobnicate", {"collect"})
    assert not command_errors("uv run ai-scholar collect", {"collect"})


def test_checker_detects_yaml11_bool_mode() -> None:
    assert mode_errors("      AI_SCHOLAR_MODE: off")
    assert mode_errors("      AI_SCHOLAR_MODE: yes")
    assert not mode_errors('      AI_SCHOLAR_MODE: "off"')


def test_checker_detects_unguarded_recap_add() -> None:
    assert recap_add_errors("          git add data/reports/weekly data/reports/monthly\n")
    assert recap_add_errors("          git add data/reports/weekly\n")  # 单路径但无 -d 守卫
    assert recap_add_errors("          # 没有 git add 的步骤\n")
    assert not recap_add_errors(
        "          for d in data/reports/weekly data/reports/monthly; do\n"
        '            if [ -d "$d" ]; then git add "$d"; fi\n'
        "          done\n"
    )


def test_checker_detects_divergent_concurrency_groups() -> None:
    assert group_errors({"daily.yml": "a", "recap.yml": "b"})
    assert group_errors({"daily.yml": None, "recap.yml": "a"})
    assert not group_errors({"daily.yml": "ai-scholar-daily", "recap.yml": "ai-scholar-daily"})


def test_checker_detects_missing_or_misordered_peak_guard() -> None:
    guard = (
        "      - name: Peak-hour guard\n        run: uv run python -m ai_scholar.utils.peak_hours\n"
    )
    assert guard_errors("      - name: Enhance\n        run: uv run ai-scholar enhance\n")
    assert guard_errors("      - name: Enhance\n        run: uv run ai-scholar enhance\n" + guard)
    good = guard + "      - name: Enhance\n        run: uv run ai-scholar enhance\n"
    assert not guard_errors(good)
