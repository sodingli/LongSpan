"""数据库引擎与会话。

M0 取舍说明：使用同步 SQLAlchemy（SQLite 本地写为毫秒级），
在 asyncio 上下文中直接调用。生产切 Postgres + 高并发时，
再把这层换成 async engine / 线程池，业务代码不受影响。
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import settings
from .models import Base

_is_sqlite = settings.database_url.startswith("sqlite")

engine = create_engine(
    settings.database_url,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
)

if _is_sqlite:
    # SQLite 并发写保护：开启 WAL，允许读写并行
    @event.listens_for(engine, "connect")
    def _sqlite_pragma(dbapi_conn, _rec):  # pragma: no cover - 驱动层回调
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def init_db() -> None:
    Base.metadata.create_all(engine)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
