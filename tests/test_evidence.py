"""Evidence 后校验测试（M3 / D9）。

覆盖：A/B 须有效 source_refs、伪造引用降级、C 弱化、N 强制「无法判断」、
doc_check 证据、violation / direct_match 降级。
"""
from __future__ import annotations

from app.domain.enums import DocCheckVerdict, EvidenceLevel, RiskLevel, RuleVerdict, TechDebtVerdict
from app.domain.schemas import (
    CheckReport, DocCheckItem, ReportMeta, RiskItem, RuleItem, TechDebtItem,
)
from app.agent.evidence import sanitize_report, validate_report

# 本次检索真实命中的知识库来源
VALID = {"kb-real-1"}


def _meta():
    return ReportMeta(pr_id=1, project="p", analysis_mode="full", kb_status="success")


def test_ab_without_refs_rejected():
    r = CheckReport(meta=_meta(), doc_check=[
        DocCheckItem(item="API文档", verdict=DocCheckVerdict.CONFIRM, basis="命中规范",
                    advice="", evidence_level=EvidenceLevel.A, source_refs=[]),
    ])
    issues = validate_report(r, valid_refs=VALID)
    assert any("缺少有效 source_refs" in i for i in issues)
    cleaned = sanitize_report(r, valid_refs=VALID)
    assert cleaned.doc_check == []  # 无据强结论被拒绝


def test_c_strong_word_flagged_and_stripped():
    r = CheckReport(meta=_meta(), doc_check=[
        DocCheckItem(item="日志", verdict=DocCheckVerdict.UNKNOWN, basis="违反项目日志规范",
                    advice="", evidence_level=EvidenceLevel.C, source_refs=[]),
    ])
    issues = validate_report(r, valid_refs=VALID)
    assert any("强结论词" in i for i in issues)
    cleaned = sanitize_report(r, valid_refs=VALID)
    assert "违反" not in cleaned.doc_check[0].basis


def test_rule_violation_without_evidence_downgraded():
    r = CheckReport(meta=_meta(), project_rules=[
        RuleItem(item="命名", verdict=RuleVerdict.VIOLATION, evidence_level=EvidenceLevel.C,
                source_refs=[]),
    ])
    cleaned = sanitize_report(r, valid_refs=VALID)
    assert cleaned.project_rules[0].verdict == RuleVerdict.UNKNOWN


def test_tech_debt_strong_without_refs_downgraded():
    r = CheckReport(meta=_meta(), tech_debt=[
        TechDebtItem(item="缓存", verdict=TechDebtVerdict.DIRECT_MATCH, evidence_level=EvidenceLevel.B,
                    source_refs=[]),
    ])
    cleaned = sanitize_report(r, valid_refs=VALID)
    assert cleaned.tech_debt[0].verdict == TechDebtVerdict.POSSIBLE


def test_valid_report_passes():
    r = CheckReport(meta=_meta(), doc_check=[
        DocCheckItem(item="API文档", verdict=DocCheckVerdict.CONFIRM, basis="检测到公共 API 新增",
                    advice="确认文档同步", evidence_level=EvidenceLevel.C, source_refs=[]),
    ])
    assert validate_report(r, valid_refs=VALID) == []


# ===== source_refs 真实性（伪造引用不得支撑强结论） =====
def test_ab_with_valid_ref_kept():
    r = CheckReport(meta=_meta(), risk=[
        RiskItem(level=RiskLevel.HIGH, text="接口签名变更", evidence_level=EvidenceLevel.A,
                source_refs=["kb-real-1"]),
    ])
    cleaned = sanitize_report(r, valid_refs=VALID)
    assert len(cleaned.risk) == 1
    assert cleaned.risk[0].evidence_level == EvidenceLevel.A
    assert cleaned.risk[0].source_refs == ["kb-real-1"]


def test_fabricated_ref_downgraded_not_dropped():
    """LLM 引用不存在的知识库 id：降级为 C + 剥离 ref，不静默丢弃该风险。"""
    r = CheckReport(meta=_meta(), risk=[
        RiskItem(level=RiskLevel.HIGH, text="新增支付回调", evidence_level=EvidenceLevel.A,
                source_refs=["kb-fabricated-999"]),
    ])
    issues = validate_report(r, valid_refs=VALID)
    assert any("不存在的知识库来源" in i for i in issues)
    cleaned = sanitize_report(r, valid_refs=VALID)
    assert len(cleaned.risk) == 1
    assert cleaned.risk[0].evidence_level == EvidenceLevel.C
    assert cleaned.risk[0].source_refs == []


def test_fabricated_ref_weakens_strong_wording():
    """伪造引用支撑的强结论措辞必须被弱化，不能原样进入报告。"""
    r = CheckReport(meta=_meta(), risk=[
        RiskItem(level=RiskLevel.HIGH, text="已确认违反团队规范，必须立即修复",
                evidence_level=EvidenceLevel.A, source_refs=["kb-nope"]),
    ])
    cleaned = sanitize_report(r, valid_refs=VALID)
    text = cleaned.risk[0].text
    assert "已确认" not in text and "必须" not in text


def test_mixed_refs_keep_only_valid():
    r = CheckReport(meta=_meta(), tech_debt=[
        TechDebtItem(item="缓存", verdict=TechDebtVerdict.RELATED, evidence_level=EvidenceLevel.B,
                    source_refs=["kb-real-1", "kb-ghost"]),
    ])
    cleaned = sanitize_report(r, valid_refs=VALID)
    assert cleaned.tech_debt[0].source_refs == ["kb-real-1"]
    assert cleaned.tech_debt[0].verdict == TechDebtVerdict.RELATED


def test_fabricated_ref_cannot_support_violation():
    """伪造引用不得让 project_rules 保留 violation 强结论。"""
    r = CheckReport(meta=_meta(), project_rules=[
        RuleItem(item="日志规范", verdict=RuleVerdict.VIOLATION, evidence_level=EvidenceLevel.A,
                source_refs=["kb-ghost"]),
    ])
    cleaned = sanitize_report(r, valid_refs=VALID)
    assert cleaned.project_rules[0].verdict == RuleVerdict.UNKNOWN
    assert cleaned.project_rules[0].evidence_level == EvidenceLevel.N


# ===== N 级强制「无法判断」 =====
def test_n_level_forced_to_say_unknown():
    r = CheckReport(meta=_meta(), risk=[
        RiskItem(level=RiskLevel.HIGH, text="已确认违反规范，必须改",
                evidence_level=EvidenceLevel.N, source_refs=[]),
    ])
    issues = validate_report(r, valid_refs=VALID)
    assert any("未明确" in i for i in issues)
    cleaned = sanitize_report(r, valid_refs=VALID)
    text = cleaned.risk[0].text
    assert text.startswith("无法判断")
    assert "已确认" not in text and "必须" not in text


def test_n_level_keeps_existing_unknown_marker():
    r = CheckReport(meta=_meta(), doc_check=[
        DocCheckItem(item="配置文档", verdict=DocCheckVerdict.UNKNOWN,
                    basis="无法判断，知识库未检索到相关信息", advice="",
                    evidence_level=EvidenceLevel.N, source_refs=[]),
    ])
    assert validate_report(r, valid_refs=VALID) == []
    cleaned = sanitize_report(r, valid_refs=VALID)
    assert cleaned.doc_check[0].basis == "无法判断，知识库未检索到相关信息"
