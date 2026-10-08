"""SQLite 存储：KB 文档元数据。

依据 C2 / D15：
- 仅保存 KB 文档 metadata；
- 原始 Diff / 完整 PR 描述 / 报告不落库（ephemeral）。
"""
from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator

from sqlmodel import Session, create_engine

from app.config import get_settings


class _EngineHolder:
    _engine = None

    @classmethod
    def get(cls):
        if cls._engine is None:
            url = get_settings().database_url
            cls._engine = create_engine(url, connect_args={"check_same_thread": False})
            # 首次获取引擎即建表：否则 kb upload/list 在生产路径会因表不存在而
            # 抛 sqlite3.OperationalError -> 顶层兜底成 INTERNAL_ERROR(99)。
            init_db()
        return cls._engine

    @classmethod
    def reset(cls):
        cls._engine = None


def init_db() -> None:
    # 延迟导入避免循环；models.py 顶部 from sqlmodel import SQLModel
    from app.domain import models as m

    engine = _EngineHolder.get()
    m.SQLModel.metadata.create_all(engine)


def get_engine():
    return _EngineHolder.get()


@contextmanager
def session_scope() -> Iterator[Session]:
    session = Session(get_engine(), expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine() -> None:
    _EngineHolder.reset()
