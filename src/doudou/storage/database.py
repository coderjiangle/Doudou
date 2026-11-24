"""Async engine, session factory and schema bootstrap."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import make_url
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


def _expand_sqlite_url(url: str) -> str:
    """把 SQLite URL 里的 ``~`` 展开成绝对路径。

    SQLAlchemy 不会展开 ``~``；默认配置写成 ``~/.doudou/data/...`` 时，
    如果原样交给引擎，SQLite 会把它当字面文件名。相对路径保持原样
    （仍按进程 cwd 解析），只规范化绝对/``~`` 路径。
    """
    if not url.startswith(_SQLITE_PREFIXES):
        return url
    try:
        parsed = make_url(url)
    except Exception:
        return url
    database = parsed.database
    if not database or database == ":memory:" or database.startswith(":memory:"):
        return url
    path = Path(database).expanduser()
    if not path.is_absolute():
        return url
    # 不要用 render_as_string：它会百分号编码 Windows 盘符，而 SQLite 的
    # 目录创建逻辑需要的是可直接 Path() 的文本路径。
    return f"{parsed.drivername}:///{path.as_posix()}"


def _ensure_sqlite_dir(url: str) -> None:
    """SQLite 文件所在目录不存在时先建出来。

    否则首次运行会以「unable to open database file」失败，而那个报错完全看不出
    是缺目录。
    """
    url = _expand_sqlite_url(url)
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
        self.database_url = _expand_sqlite_url(config.database_url)
        _ensure_sqlite_dir(self.database_url)
        self.engine: AsyncEngine = create_async_engine(
            self.database_url,
            echo=config.echo_sql,
            # SQLite 默认在同一条连接上复用，异步场景需要放开。
            pool_pre_ping=not self.database_url.startswith("sqlite"),
        )
        if self.database_url.startswith("sqlite"):
            self._configure_sqlite()
        self.session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine, expire_on_commit=False
        )

    def _configure_sqlite(self) -> None:
        """SQLite 的开关：外键约束 + 并发写参数。

        **外键**：SQLite 默认不强制外键，于是 ``ondelete="CASCADE"`` 形同虚设 ——
        删了 thread，messages 会留下成为孤儿行。

        **并发**（压测实测出来的，见 ``docs/performance.md``）：

        * ``journal_mode=WAL`` —— 默认的回滚日志会让**写操作阻塞所有读**，
          而我们是「每轮若干次读 + 若干次写」的混合负载，16 并发下实测吞吐
          只有 6 次/秒；WAL 之后读不再被写挡住；
        * ``synchronous=NORMAL`` —— WAL 下每次提交仍要 fsync，而一轮运行有
          五六次提交，「每次提交都等落盘」于是成了主导开销；``NORMAL`` 只牺牲
          「断电时可能丢掉最后几个已提交事务」，不会损坏数据库；
        * ``busy_timeout`` —— 撞锁时等待，而不是立刻抛 "database is locked"。

        这些只对 SQLite 生效；换 Postgres 时整个方法不会执行。
        """
        pragmas = ["PRAGMA foreign_keys=ON"]
        if ":memory:" not in self.database_url:
            pragmas += [
                "PRAGMA journal_mode=WAL",
                "PRAGMA synchronous=NORMAL",
                "PRAGMA busy_timeout=5000",
            ]

        @event.listens_for(self.engine.sync_engine, "connect")
        def _set_pragma(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            for statement in pragmas:
                cursor.execute(statement)
            cursor.close()

    async def create_schema(self) -> None:
        """建表。

        用 ``create_all`` 就够：schema 还是全新的、没有线上数据要迁移。
        等第一次真正需要改表结构时再引入 Alembic（见 storage/migrations/README.md）。
        """
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        logger.info("storage.schema_ready", url=_safe_url(self.database_url))

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
