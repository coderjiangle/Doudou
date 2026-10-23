"""Async engine, session factory and schema bootstrap."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from doudou.config.schema import StorageConfig
from doudou.observability.logging import get_logger
from doudou.storage.models import Base

logger = get_logger(__name__)

_SQLITE_PREFIXES = ("sqlite+aiosqlite:///", "sqlite+aiosqlite://")


def _ensure_sqlite_dir(url: str) -> None:
    """SQLite 文件所在目录不存在时先建出来。

    否则首次运行会以「unable to open database file」失败，而那个报错完全看不出
    是缺目录。
    """
    if not url.startswith(_SQLITE_PREFIXES):
        return
    raw = url.split("///", 1)[1] if "///" in url else ""
    if not raw or raw == ":memory:" or raw.startswith(":memory:"):
        return
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)


class Database:
    """数据库句柄：引擎 + 会话工厂 + 建表。"""

    def __init__(self, config: StorageConfig) -> None:
        self.config = config
        _ensure_sqlite_dir(config.database_url)
        self.engine: AsyncEngine = create_async_engine(
            config.database_url,
            echo=config.echo_sql,
            # SQLite 默认在同一条连接上复用，异步场景需要放开。
            pool_pre_ping=not config.database_url.startswith("sqlite"),
        )
        if config.database_url.startswith("sqlite"):
            self._enable_sqlite_foreign_keys()
        self.session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine, expire_on_commit=False
        )

    def _enable_sqlite_foreign_keys(self) -> None:
        """打开 SQLite 的外键约束。

        SQLite 默认**不**强制外键，于是 ``ondelete="CASCADE"`` 形同虚设 ——
        删了 thread，messages 会留下成为孤儿行。Postgres 无此问题，所以这里只对
        SQLite 生效。
        """

        @event.listens_for(self.engine.sync_engine, "connect")
        def _set_pragma(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    async def create_schema(self) -> None:
        """建表。

        M2 用 ``create_all`` 就够：schema 还是全新的、没有线上数据要迁移。
        等第一次真正需要改表结构时再引入 Alembic（见 storage/migrations/README.md）。
        """
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        logger.info("storage.schema_ready", url=_safe_url(self.config.database_url))

    async def dispose(self) -> None:
        await self.engine.dispose()
        logger.debug("storage.disposed")

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """开一个会话；异常时回滚，正常时提交。

        usage::

            async with db.session() as session:
                session.add(obj)
        """
        async with self.session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise


def _safe_url(url: str) -> str:
    """日志里去掉 URL 中的口令部分。"""
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    _, _, host = rest.rpartition("@")
    return f"{scheme}://***@{host}"


def affected_rows(result: Any) -> int:
    """取 DML（delete / update）的影响行数。

    SQLAlchemy 的 ``execute`` 在类型层面把 ``Delete`` / ``Update`` 归到泛型
    ``Result`` 重载上，于是 mypy 看不到 ``rowcount``；运行期它其实是 ``CursorResult``。
    与其在各处撒 ``type: ignore``，不如在这里统一收敛。
    """
    return int(getattr(result, "rowcount", 0) or 0)


def create_database(config: StorageConfig) -> Database:
    """按配置构造数据库句柄。"""
    return Database(config)


__all__ = ["Database", "affected_rows", "create_database"]
