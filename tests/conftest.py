"""pytest fixtures：注入 Fake 适配器、临时 SQLite、临时主密钥。"""
from __future__ import annotations

import os
import tempfile

# 必须在导入 app 之前设置环境
os.environ.setdefault("PR_CHECK_USE_FAKE", "1")
_tmp = tempfile.mkdtemp(prefix="pr_check_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_tmp, 'test.db')}"

import pytest  # noqa: E402

import app.config  # noqa: E402
from app.container import get_container, reset_container, set_container  # noqa: E402
from app.storage.sqlite import init_db, reset_engine  # noqa: E402
from app.adapters.fakes import FakeKB, FakeLLM  # noqa: E402


@pytest.fixture
def container():
    reset_engine()
    reset_container()
    app.config.get_settings.cache_clear()
    init_db()
    # 测试隔离：清空 KB 文档表，避免共享临时 SQLite 数据串扰
    from app.domain.models import KbDoc
    from app.storage.repo import session_scope
    from sqlmodel import delete
    with session_scope() as s:
        s.exec(delete(KbDoc))
    c = get_container()
    c.llm = FakeLLM()
    c.kb = FakeKB()
    set_container(c)
    return c
