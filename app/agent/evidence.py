"""Evidence 后校验（M3 / D9）。

强制规则：
- A / B 级结论必须带非空 source_refs，否则拒绝该强结论。
- C 级只能「建议关注 / 建议确认 / 建议人工检查」，禁止强结论词。
- N 级必须转「无法判断」。
- doc_check 也必须具备 Evidence（与 project_rules / tech_debt / risk 一致）。
project_rules 的 violation（违反规范）与 tech_debt 的 direct_match/related 均属项目特定强结论，
必须有 A/B 证据，否则降级。
"""
from __future__ import annotations

from app.domain.enums import EvidenceLevel
from app.domain.schemas import (
    CheckReport, DocCheckItem, RiskItem, RuleItem, TechDebtItem,
)

FORBIDDEN_STRONG_WORDS = ["违反", "命中", "已确认", "必须", "一定", "肯定", "明确需要"]

_A_B = {EvidenceLevel.A, EvidenceLevel.B}


def _strip_strong_words(text: str) -> str:
    out = text
    for w in FORBIDDEN_STRONG_WORDS:
        out = out.replace(w, "建议确认")
    return out


def validate_report(report: CheckReport) -> list[str]:
    """返回违规说明列表；空列表表示通过。"""
    issues: list[str] = []

    def _check(item, section: str):
        el = item.evidence_level
        refs = item.source_refs
        if el in _A_B and not refs:
            issues.append(f"[{section}] {getattr(item, 'item', '?')} 证据等级 {el.value} 缺少 source_refs")
        if el == EvidenceLevel.C:
            text = " ".join(str(x) for x in (
                getattr(item, "basis", ""), getattr(item, "advice", ""),
                getattr(item, "text", ""),
            ))
            if any(w in text for w in FORBIDDEN_STRONG_WORDS):
                issues.append(f"[{section}] {getattr(item, 'item', '?')} 为 C 级却含强结论词")

    for it in report.doc_check:
        _check(it, "doc_check")
    for it in report.risk:
        _check(it, "risk")
    for it in report.project_rules:
        _check(it, "project_rules")
        if it.verdict.value == "violation" and it.evidence_level not in _A_B:
            issues.append(f"[project_rules] {it.item} 判定为 violation 但证据非 A/B")
    for it in report.tech_debt:
        _check(it, "tech_debt")
        if it.verdict.value in ("direct_match", "related") and not it.source_refs:
            issues.append(f"[tech_debt] {it.item} 判定为 {it.verdict.value} 但缺少 source_refs")
    return issues


def sanitize_report(report: CheckReport) -> CheckReport:
    """修正违反 Evidence 规则的条目：丢弃无据强结论 / 降级 / 弱化措辞。"""
    doc_check, risk, project_rules, tech_debt = [], [], [], []

    for it in report.doc_check:
        if it.evidence_level in _A_B and not it.source_refs:
            continue  # 拒绝无据强结论
        if it.evidence_level == EvidenceLevel.C:
            it = DocCheckItem(
                item=it.item, verdict=it.verdict, basis=_strip_strong_words(it.basis),
                advice=_strip_strong_words(it.advice), evidence_level=it.evidence_level,
                source_refs=it.source_refs,
            )
        doc_check.append(it)

    for it in report.risk:
        if it.evidence_level in _A_B and not it.source_refs:
            continue
        if it.evidence_level == EvidenceLevel.C:
            it = RiskItem(level=it.level, text=_strip_strong_words(it.text),
                         evidence_level=it.evidence_level, source_refs=it.source_refs)
        risk.append(it)

    for it in report.project_rules:
        if it.verdict.value == "violation" and it.evidence_level not in _A_B:
            # 无证据不得声称违反规范
            it = RuleItem(item=it.item, verdict="unknown", evidence_level=EvidenceLevel.N,
                         source_refs=it.source_refs)
        elif it.evidence_level in _A_B and not it.source_refs:
            continue
        project_rules.append(it)

    for it in report.tech_debt:
        if it.verdict.value in ("direct_match", "related") and not it.source_refs:
            it = TechDebtItem(item=it.item, verdict="possible", evidence_level=EvidenceLevel.C,
                             source_refs=it.source_refs)
        elif it.evidence_level in _A_B and not it.source_refs:
            continue
        tech_debt.append(it)

    return CheckReport(
        meta=report.meta, summary=report.summary, doc_check=doc_check,
        risk=risk, project_rules=project_rules, tech_debt=tech_debt,
        manual_checklist=report.manual_checklist, kb_sources=report.kb_sources,
    )
