"""Evidence 后校验测试（M3 / D9）。

覆盖：A/B 须有效 source_refs、伪造引用降级、C 弱化、N 强制「无法判断」、
doc_check 证据、violation / direct_match 降级。
"""
from __future__ import annotations

import pytest

from app.domain.enums import DocCheckVerdict, EvidenceLevel, RiskLevel, RuleVerdict, TechDebtVerdict
from app.domain.schemas import (
    CheckReport, DocCheckItem, ReportMeta, RiskItem, RuleItem, TechDebtItem,
)
from app.agent.evidence import sanitize_report, validate_report

# 本次检索真实命中的知识库来源
VALID = {"kb-real-1"}
# 来源类型（规则 4）。real-world 场景：Dify 上同时存在四类文档，
# LLM 可能拿《代码风格》去支撑技术债务判定——来源真实命中但内容无关。
TYPES = {
    "kb-real-1": "api_document",
    "kb-rule": "development_rule",
    "kb-debt": "technical_debt",
    "kb-risk": "historical_risk",
    "kb-unknown": "",
}


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
        TechDebtItem(item="缓存", verdict=TechDebtVerdict.DIRECT_MATCH,
                     evidence_level=EvidenceLevel.B,
                     source_refs=[]),
    ])
    cleaned = sanitize_report(r, valid_refs=VALID)
    assert cleaned.tech_debt[0].verdict == TechDebtVerdict.POSSIBLE


def test_sanitize_risk_keeps_location():
    """location 是定位指针不是结论：Evidence 清洗不得剥离该字段。"""
    r = CheckReport(meta=_meta(), risk=[
        RiskItem(level=RiskLevel.HIGH, text="注意事务", location="src/pay/Refund.java:42",
                 evidence_level=EvidenceLevel.C, source_refs=[]),
    ])
    cleaned = sanitize_report(r, valid_refs=VALID)
    assert cleaned.risk[0].location == "src/pay/Refund.java:42"


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


# ===== 规则 4：来源类型必须能支撑所在段落 =====
#
# 真实运行暴露的场景：tech_debt 的 direct_match 引用了《代码风格与结构规范》
# （development_rule）。来源真实命中，因此规则 1-3 全部放行——但一份讲命名与目录
# 的文档不可能支撑「Java 正则捕获组写错」这种技术债务判定。伪造 id 拦得住，
# 这种「真实 id + 无关内容」的凑数引用拦不住。
_ALL = set(TYPES)


@pytest.mark.parametrize("section,builder,bad_ref,good_ref", [
    ("doc_check", lambda ref, lv: DocCheckItem(
        item="API 文档", verdict=DocCheckVerdict.CONFIRM, basis="接口签名不一致",
        advice="", evidence_level=lv, source_refs=[ref]),
     "kb-rule", "kb-real-1"),
    ("project_rules", lambda ref, lv: RuleItem(
        item="命名约定", verdict=RuleVerdict.OK, evidence_level=lv,
        source_refs=[ref]),
     "kb-debt", "kb-rule"),
    ("tech_debt", lambda ref, lv: TechDebtItem(
        item="Java 正则捕获组错误", verdict=TechDebtVerdict.RELATED,
        evidence_level=lv, source_refs=[ref]),
     "kb-rule", "kb-debt"),
    ("risk", lambda ref, lv: RiskItem(
        level=RiskLevel.HIGH, text="注意事务边界", evidence_level=lv,
        source_refs=[ref]),
     "kb-real-1", "kb-debt"),
])
def test_wrong_doc_type_downgrades_strong_level(section, builder, bad_ref, good_ref):
    """真实命中但类型与段落无关时，A/B 必须降为 C。"""
    r = CheckReport(meta=_meta(), **{section: [builder(bad_ref, EvidenceLevel.B)]})
    cleaned = sanitize_report(r, valid_refs=_ALL, ref_doc_types=TYPES)
    item = getattr(cleaned, section)[0]
    assert item.evidence_level == EvidenceLevel.C, section


@pytest.mark.parametrize("section,builder,good_ref", [
    ("doc_check", lambda ref, lv: DocCheckItem(
        item="API 文档", verdict=DocCheckVerdict.CONFIRM, basis="接口签名不一致",
        advice="", evidence_level=lv, source_refs=[ref]), "kb-real-1"),
    ("project_rules", lambda ref, lv: RuleItem(
        item="命名约定", verdict=RuleVerdict.OK, evidence_level=lv,
        source_refs=[ref]), "kb-rule"),
    ("tech_debt", lambda ref, lv: TechDebtItem(
        item="缓存未失效", verdict=TechDebtVerdict.DIRECT_MATCH,
        evidence_level=lv, source_refs=[ref]), "kb-debt"),
    ("risk", lambda ref, lv: RiskItem(
        level=RiskLevel.HIGH, text="事务边界缺失", evidence_level=lv,
        source_refs=[ref]), "kb-risk"),
])
def test_matching_doc_type_keeps_strong_level(section, builder, good_ref):
    """类型契合时 A/B 不受影响——规则 4 不能误伤合规引用。"""
    r = CheckReport(meta=_meta(), **{section: [builder(good_ref, EvidenceLevel.B)]})
    cleaned = sanitize_report(r, valid_refs=_ALL, ref_doc_types=TYPES)
    assert getattr(cleaned, section)[0].evidence_level == EvidenceLevel.B, section


def test_unknown_doc_type_cannot_support_strong_level():
    """来源 doc_type 未知（如非本工具上传、无法解析归属）不得支撑 A/B。

    严格处理：无法证明来源类型，就不能让它支撑强结论。
    """
    r = CheckReport(meta=_meta(), tech_debt=[TechDebtItem(
        item="缓存未失效", verdict=TechDebtVerdict.DIRECT_MATCH,
        evidence_level=EvidenceLevel.B, source_refs=["kb-unknown"])])
    cleaned = sanitize_report(r, valid_refs=_ALL, ref_doc_types=TYPES)
    item = cleaned.tech_debt[0]
    assert item.evidence_level == EvidenceLevel.C
    # 判定必须一并降级：闸门按 verdict 求值，只降 level 会让 C 级证据继续阻断
    assert item.verdict == TechDebtVerdict.POSSIBLE


def test_incompatible_refs_stripped_when_compatible_one_present():
    """部分引用类型契合时保留强结论，并剥离类型不相关的引用。"""
    r = CheckReport(meta=_meta(), tech_debt=[TechDebtItem(
        item="缓存未失效", verdict=TechDebtVerdict.DIRECT_MATCH,
        evidence_level=EvidenceLevel.B, source_refs=["kb-rule", "kb-debt"])])
    cleaned = sanitize_report(r, valid_refs=_ALL, ref_doc_types=TYPES)
    item = cleaned.tech_debt[0]
    assert item.evidence_level == EvidenceLevel.B
    assert item.source_refs == ["kb-debt"]


def test_weak_level_untouched_by_doc_type_rule():
    """C 级条目不依赖来源，规则 4 不得改动其等级与引用。"""
    r = CheckReport(meta=_meta(), tech_debt=[TechDebtItem(
        item="缓存", verdict=TechDebtVerdict.POSSIBLE, evidence_level=EvidenceLevel.C,
        source_refs=["kb-rule"])])
    cleaned = sanitize_report(r, valid_refs=_ALL, ref_doc_types=TYPES)
    item = cleaned.tech_debt[0]
    assert item.evidence_level == EvidenceLevel.C
    assert item.source_refs == ["kb-rule"]


def test_rule4_skipped_when_no_doc_types_supplied():
    """不提供 ref_doc_types 时跳过规则 4（兼容不感知类型的旧调用方）。"""
    r = CheckReport(meta=_meta(), tech_debt=[TechDebtItem(
        item="缓存", verdict=TechDebtVerdict.DIRECT_MATCH, evidence_level=EvidenceLevel.B,
        source_refs=["kb-rule"])])
    cleaned = sanitize_report(r, valid_refs=_ALL)
    assert cleaned.tech_debt[0].evidence_level == EvidenceLevel.B


def test_validate_report_flags_wrong_doc_type():
    """validate_report 必须能报出类型错配，否则降级过程不可观测。"""
    r = CheckReport(meta=_meta(), tech_debt=[TechDebtItem(
        item="Java 正则捕获组错误", verdict=TechDebtVerdict.DIRECT_MATCH,
        evidence_level=EvidenceLevel.B, source_refs=["kb-rule"])])
    issues = validate_report(r, valid_refs=_ALL, ref_doc_types=TYPES)
    assert any("来源类型不支持该段落" in i for i in issues), issues
