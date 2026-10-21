"""structlog 日志配置：控制台走 rich，文件落 JSON Lines。

- 控制台：给人看的（开发 / CI 排障），用 rich 高亮；
- 文件：`data/logs/ai_scholar.jsonl`，一行一条 JSON，便于事后 grep / jq 分析。
- 代码内禁止 `print`；面向用户的 CLI 输出走 `rich.console.Console`。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import structlog
from rich.console import Console
from rich.logging import RichHandler

_CONFIGURED = False
_DEFAULT_LOG_FILE = "ai_scholar.jsonl"


def _shared_processors() -> list[structlog.typing.Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]


def configure_logging(
    level: str = "INFO",
    log_dir: Path | None = None,
    *,
    console: bool = True,
    console_stderr: bool = False,
) -> None:
    """初始化日志。可重复调用（会重置 handlers），pytest 里建议传 log_dir=tmp_path。"""
    global _CONFIGURED

    processors = _shared_processors()
    structlog.configure(
        processors=[*processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(level.upper())

    if console:
        rich_handler = RichHandler(
            level=level.upper(),
            console=Console(stderr=console_stderr),
            rich_tracebacks=True,
            markup=True,
            show_time=False,
            show_path=False,
        )
        rich_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                foreign_pre_chain=processors,
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.dev.ConsoleRenderer(colors=False),
                ],
            )
        )
        root.addHandler(rich_handler)

    if log_dir is not None:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_dir / _DEFAULT_LOG_FILE, encoding="utf-8")
        file_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                foreign_pre_chain=processors,
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.processors.dict_tracebacks,
                    structlog.processors.JSONRenderer(),
                ],
            )
        )
        root.addHandler(file_handler)

    _CONFIGURED = True


def get_logger(name: str | None = None) -> Any:
    """取 logger。未显式配置过时按默认（INFO + 控制台）初始化一次。"""
    if not _CONFIGURED:
        configure_logging()
    return structlog.get_logger(name)
