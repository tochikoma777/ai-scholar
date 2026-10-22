"""数据库引擎与初始化。

★ aipulse 坑 6：Actions 里 `data/` 目录不存在会导致建库失败 ——
所以**建库前必须 mkdir**（`ensure_data_dir()`）。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlmodel import Session, SQLModel

from ai_scholar.config import Settings, load_settings


def default_db_path(settings: Settings | None = None) -> Path:
    """数据库文件路径（默认 `data/ai_scholar.db`）。"""
    return (settings or load_settings()).db_path()


def ensure_data_dir(db_path: Path) -> Path:
    """★ 建库前确保父目录存在（空 `data/` 也能 `alembic upgrade head`）。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return db_path


def engine_for(db_path: Path | None = None, settings: Settings | None = None) -> Engine:
    path = ensure_data_dir(db_path or default_db_path(settings))
    return create_engine(f"sqlite:///{path}", echo=False)


def init_db(engine: Engine) -> None:
    """建表（测试与本地快速初始化用；生产迁移走 `alembic upgrade head`）。

    索引在 `create_all` 时由 `__table_args__` 建立（见 models 里的 Index）。
    """
    SQLModel.metadata.create_all(engine)


def session_for(engine: Engine) -> Session:
    """取一个 session。"""
    return Session(engine)
