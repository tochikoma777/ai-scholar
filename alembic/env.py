"""Alembic 环境。

★ aipulse 坑 6：Actions 里 `data/` 不存在会导致建库失败 ——
所以这里在连接前**先把数据库文件所在目录建出来**。
"""

from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import Engine, engine_from_config, pool
from sqlmodel import SQLModel

import ai_scholar.store.models  # noqa: F401 —— 导入即注册 metadata

config = context.config
target_metadata = SQLModel.metadata

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _ensure_parent_dir(url: str) -> None:
    """sqlite 文件路径的父目录必须存在（空 `data/` 下也要能 upgrade）。"""
    prefix = "sqlite:///"
    if url.startswith(prefix):
        path = Path(url[len(prefix) :])
        if str(path) not in ("", ":memory:") and path.parent != Path(""):
            path.parent.mkdir(parents=True, exist_ok=True)


def _db_url() -> str:
    """数据库 URL：`AI_SCHOLAR_DB_URL` 可覆盖（便于在临时目录验证迁移）。"""
    return os.getenv("AI_SCHOLAR_DB_URL") or str(config.get_main_option("sqlalchemy.url"))


def _engine() -> Engine:
    url = _db_url()
    _ensure_parent_dir(url)
    section = dict(config.get_section(config.config_ini_section, {}))
    section["sqlalchemy.url"] = url
    return engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)


def run_migrations_offline() -> None:
    context.configure(
        url=_db_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # sqlite 改表需要 batch 模式
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = _engine()
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # sqlite 改表需要 batch 模式
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
