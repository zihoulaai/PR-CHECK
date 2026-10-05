"""KBQuery Builder 测试（M2）：focus 推导与字段上限。"""
from __future__ import annotations

from app.agent.kb_query import build_kb_query
from app.domain.enums import ChangeType
from app.domain.schemas import ChangeProfile, FileChange, PRMetadata, Symbol


def _profile(change_types, keywords=None):
    return ChangeProfile(
        changed_files=1, modules=["refund"], files=[FileChange(path="a.java", status="added")],
        symbols=[Symbol(name="RefundController", kind="class")],
        change_types=change_types, keywords=keywords or ["refund"],
    )


def test_focus_default_and_api():
    pr = PRMetadata(project="order-service", title="增加退款接口")
    q = build_kb_query(pr, _profile([ChangeType.API_CHANGE]))
    assert "doc_sync" in q.focus
    assert "development_rule" in q.focus
    assert "api_document" in q.focus
    assert q.project == "order-service"


def test_focus_business_adds_debt_risk():
    pr = PRMetadata(project="order-service", title="改数据库")
    q = build_kb_query(pr, _profile([ChangeType.DATABASE_CHANGE]))
    assert "technical_debt" in q.focus
    assert "historical_risk" in q.focus


def test_field_caps():
    pr = PRMetadata(project="order-service", title="x" * 500)
    big_kw = [f"k{i}" for i in range(100)]
    q = build_kb_query(pr, _profile([ChangeType.API_CHANGE], keywords=big_kw))
    assert len(q.pr_title) <= 200
    assert len(q.keywords) <= 20
    assert len(q.key_files) <= 20
    assert len(q.key_symbols) <= 30
