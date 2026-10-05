"""KBQuery Builder 测试（M2）：focus 推导、字段上限与查询文本。"""
from __future__ import annotations

from app.adapters.maas_kb import _build_query_text
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


def test_change_type_file_lists_passed_through():
    """各变更类型的文件清单直接来自 ChangeProfile，不再按路径另做推断。"""
    prof = ChangeProfile(
        changed_files=4, modules=["refund"],
        files=[FileChange(path=f"src/refund/F{i}.java", status="modified") for i in range(4)],
        change_types=[ChangeType.API_CHANGE, ChangeType.CONFIG_CHANGE,
                      ChangeType.DEPENDENCY_CHANGE, ChangeType.LOGGING_CHANGE],
        api_changes=["src/refund/F0.java"],
        config_changes=["src/refund/F1.yml"],
        dependency_changes=["pom.xml"],
        logging_changes=["src/refund/F2.java"],
    )
    q = build_kb_query(PRMetadata(project="order-service"), prof)
    assert q.api_changes == ["src/refund/F0.java"]
    assert q.config_changes == ["src/refund/F1.yml"]
    assert q.dependency_changes == ["pom.xml"]
    assert q.logging_changes == ["src/refund/F2.java"]


def test_query_text_contains_change_type_file_lists():
    """KB 服务端只强制按 project 过滤，文件清单必须落到 query 文本里才有效。"""
    prof = ChangeProfile(
        changed_files=2, modules=["refund"],
        files=[FileChange(path="src/refund/RefundController.java", status="modified")],
        change_types=[ChangeType.API_CHANGE],
        api_changes=["src/refund/RefundController.java"],
        config_changes=["src/refund/application.yml"],
        dependency_changes=["pom.xml"],
        symbols=[Symbol(name="RefundController", kind="class")],
        keywords=["refund"],
    )
    q = build_kb_query(PRMetadata(project="order-service", title="退款接口"), prof)
    text = _build_query_text(q)
    assert "src/refund/RefundController.java" in text
    assert "src/refund/application.yml" in text
    assert "pom.xml" in text
    assert "RefundController" in text


def test_query_text_omits_empty_sections():
    text = _build_query_text(build_kb_query(
        PRMetadata(project="p", title="t"), _profile([ChangeType.API_CHANGE])))
    assert "依赖变更文件" not in text
    assert "日志变更文件" not in text
    assert "项目：p" in text
