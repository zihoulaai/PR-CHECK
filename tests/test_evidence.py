"""Evidence 后校验测试（M3 / D9）：A/B 须 source_refs、C 弱化、N 无法判断、doc_check 证据。"""
from __future__ import annotations

from app.domain.enums import DocCheckVerdict, EvidenceLevel, RuleVerdict, TechDebtVerdict
from app.domain.schemas import (
    CheckReport, DocCheckItem, ReportMeta, RiskItem, RuleItem, TechDebtItem,
)
from app.agent.evidence import sanitize_report, validate_report


def _meta():
    return ReportMeta(pr_id=1, project="p", analysis_mode="full", kb_status="success")


def test_ab_without_refs_rejected():
    r = CheckReport(meta=_meta(), doc_check=[
        DocCheckItem(item="API文档", verdict=DocCheckVerdict.CONFIRM, basis="命中规范",
                    advice="", evidence_level=EvidenceLevel.A, source_refs=[]),
    ])
    issues = validate_report(r)
    assert any("缺少 source_refs" in i for i in issues)
    cleaned = sanitize_report(r)
    assert cleaned.doc_check == []  # 无据强结论被拒绝


def test_c_strong_word_flagged_and_stripped():
    r = CheckReport(meta=_meta(), doc_check=[
        DocCheckItem(item="日志", verdict=DocCheckVerdict.UNKNOWN, basis="违反项目日志规范",
                    advice="", evidence_level=EvidenceLevel.C, source_refs=[]),
    ])
    issues = validate_report(r)
    assert any("强结论词" in i for i in issues)
    cleaned = sanitize_report(r)
    assert "违反" not in cleaned.doc_check[0].basis


def test_rule_violation_without_evidence_downgraded():
    r = CheckReport(meta=_meta(), project_rules=[
        RuleItem(item="命名", verdict=RuleVerdict.VIOLATION, evidence_level=EvidenceLevel.C,
                source_refs=[]),
    ])
    cleaned = sanitize_report(r)
    assert cleaned.project_rules[0].verdict == RuleVerdict.UNKNOWN


def test_tech_debt_strong_without_refs_downgraded():
    r = CheckReport(meta=_meta(), tech_debt=[
        TechDebtItem(item="缓存", verdict=TechDebtVerdict.DIRECT_MATCH, evidence_level=EvidenceLevel.B,
                    source_refs=[]),
    ])
    cleaned = sanitize_report(r)
    assert cleaned.tech_debt[0].verdict == TechDebtVerdict.POSSIBLE


def test_valid_report_passes():
    r = CheckReport(meta=_meta(), doc_check=[
        DocCheckItem(item="API文档", verdict=DocCheckVerdict.CONFIRM, basis="检测到公共 API 新增",
                    advice="确认文档同步", evidence_level=EvidenceLevel.C, source_refs=[]),
    ])
    assert validate_report(r) == []
