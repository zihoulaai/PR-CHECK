"""Evidence 后校验（M3 / D9）。

强制规则（sanitize_report 中按序执行）：
1. source_refs 必须是本次检索真实命中的知识库 id；无效 id 一律剔除。
2. A / B 级结论若无任何 source_refs（LLM 主动承认无证据），丢弃该条目。
3. A / B 级结论若只引用了不存在的来源（LLM 伪造证据），降级为 C + 剥离 ref + 弱化措辞。
   —— 2 与 3 处置不同：伪造引用通常伴随真实观察，降级保留信息比丢弃更有价值。
4. project_rules 的 violation 与 tech_debt 的 direct_match/related 均属项目特定强结论，
   必须有 A/B 证据，否则降级。
5. C / N 级只能弱化表述，禁止强结论词。
6. N 级文本必须明确「无法判断」。

valid_refs 为「本次允许引用的知识库 id 集合」，由 workflow 从 kb_hits 传入；
它是 keyword-only 必填参数，强制所有调用方显式声明来源范围，避免漏改。
"""
from __future__ import annotations

from app.domain.enums import EvidenceLevel, RuleVerdict, TechDebtVerdict
from app.domain.schemas import CheckReport

FORBIDDEN_STRONG_WORDS = ["违反", "命中", "已确认", "必须", "一定", "肯定", "明确需要"]

UNKNOWN_MARKER = "无法判断"

# 强结论等级（须有可溯源来源）与弱结论等级（只能弱化表述）
_STRONG_LEVELS = {EvidenceLevel.A, EvidenceLevel.B}
_WEAK_LEVELS = {EvidenceLevel.C, EvidenceLevel.N}


def _strip_strong_words(text: str) -> str:
    out = text
    for w in FORBIDDEN_STRONG_WORDS:
        out = out.replace(w, "建议确认")
    return out


def _normalize_text(text: str, level: EvidenceLevel) -> str:
    """按证据等级规范化文本：C/N 剥离强结论词；N 额外确保出现「无法判断」。"""
    if level not in _WEAK_LEVELS:
        return text
    out = _strip_strong_words(text)
    if level == EvidenceLevel.N and UNKNOWN_MARKER not in out:
        out = f"{UNKNOWN_MARKER}：{out}" if out else UNKNOWN_MARKER
    return out


def _resolve_evidence(item, valid_refs: set[str]) -> tuple[list[str], EvidenceLevel] | None:
    """返回 (有效 refs, 最终证据等级)；None 表示该条目应被丢弃。

    规则 1-3 的唯一实现，四个段落共用，避免各段处置不一致。
    """
    kept = [r for r in item.source_refs if r in valid_refs]
    if item.evidence_level not in _STRONG_LEVELS:
        return kept, item.evidence_level
    if kept:
        return kept, item.evidence_level
    # A/B 但无有效引用：区分「本就无证据」与「引用全部伪造」
    if not item.source_refs:
        return None  # 规则 2：主动承认无证据的强结论，丢弃
    return kept, EvidenceLevel.C  # 规则 3：伪造引用，降级保留


def validate_report(report: CheckReport, *, valid_refs: set[str]) -> list[str]:
    """返回违规说明列表；空列表表示通过。valid_refs 见模块 docstring。"""
    issues: list[str] = []

    def _label(item) -> str:
        return getattr(item, "item", None) or getattr(item, "text", "") or "?"

    def _check(item, section: str) -> None:
        label = _label(item)
        el = item.evidence_level
        unknown_refs = [r for r in item.source_refs if r not in valid_refs]
        if unknown_refs:
            issues.append(f"[{section}] {label} 引用了不存在的知识库来源：{unknown_refs}")
        # 规则 1-3 的判定复用 _resolve_evidence，与 sanitize_report 保持一致
        resolved = _resolve_evidence(item, valid_refs)
        if el in _STRONG_LEVELS and not (resolved[0] if resolved else []):
            issues.append(f"[{section}] {label} 证据等级 {el.value} 缺少有效 source_refs")
        text = " ".join(str(x) for x in (
            getattr(item, "basis", ""), getattr(item, "advice", ""),
            getattr(item, "text", ""),
        ))
        if el in _WEAK_LEVELS:
            if any(w in text for w in FORBIDDEN_STRONG_WORDS):
                issues.append(f"[{section}] {label} 为 {el.value} 级却含强结论词")
        if el == EvidenceLevel.N and UNKNOWN_MARKER not in text:
            issues.append(f"[{section}] {label} 为 N 级但未明确「{UNKNOWN_MARKER}」")

    for it in report.doc_check:
        _check(it, "doc_check")
    for it in report.risk:
        _check(it, "risk")
    for it in report.project_rules:
        _check(it, "project_rules")
        if it.verdict.value == "violation" and it.evidence_level not in _STRONG_LEVELS:
            issues.append(f"[project_rules] {it.item} 判定为 violation 但证据非 A/B")
    for it in report.tech_debt:
        _check(it, "tech_debt")
        if it.verdict.value in ("direct_match", "related") and not any(
                r in valid_refs for r in it.source_refs):
            issues.append(f"[tech_debt] {it.item} 判定为 {it.verdict.value} 但缺少有效 source_refs")
    return issues


def sanitize_report(report: CheckReport, *, valid_refs: set[str]) -> CheckReport:
    """修正违反 Evidence 规则的条目：剔除伪造引用 / 丢弃无据强结论 / 降级 / 弱化措辞。

    调用方必须传入本次检索真实命中的 id 集合（valid_refs）；未命中的引用会被剔除，
    从而杜绝 LLM 凭空编造知识库 id 支撑 A/B 级强结论。
    """
    doc_check, risk, project_rules, tech_debt = [], [], [], []

    for it in report.doc_check:
        resolved = _resolve_evidence(it, valid_refs)
        if resolved is None:
            continue
        refs, level = resolved
        doc_check.append(it.model_copy(update={
            "basis": _normalize_text(it.basis, level),
            "advice": _normalize_text(it.advice, level),
            "evidence_level": level, "source_refs": refs,
        }))

    for it in report.risk:
        resolved = _resolve_evidence(it, valid_refs)
        if resolved is None:
            continue
        refs, level = resolved
        risk.append(it.model_copy(update={
            "text": _normalize_text(it.text, level),
            "evidence_level": level, "source_refs": refs,
        }))

    for it in report.project_rules:
        resolved = _resolve_evidence(it, valid_refs)
        if resolved is None:
            continue
        refs, level = resolved
        item_text = _normalize_text(it.item, level)
        if it.verdict.value == "violation" and level not in _STRONG_LEVELS:
            # 无证据不得声称违反规范
            project_rules.append(it.model_copy(update={
                "item": item_text, "verdict": RuleVerdict.UNKNOWN,
                "evidence_level": EvidenceLevel.N, "source_refs": refs,
            }))
        else:
            project_rules.append(it.model_copy(update={
                "item": item_text, "evidence_level": level, "source_refs": refs,
            }))

    for it in report.tech_debt:
        # direct_match / related 本身即项目特定强结论：无有效证据时降级为
        # possible / C 而非丢弃（保留 LLM 观察到的风险线索）。
        if it.verdict.value in ("direct_match", "related") \
                and not any(r in valid_refs for r in it.source_refs):
            tech_debt.append(it.model_copy(update={
                "item": _normalize_text(it.item, EvidenceLevel.C),
                "verdict": TechDebtVerdict.POSSIBLE,
                "evidence_level": EvidenceLevel.C,
                "source_refs": [r for r in it.source_refs if r in valid_refs],
            }))
            continue
        resolved = _resolve_evidence(it, valid_refs)
        if resolved is None:
            continue
        refs, level = resolved
        tech_debt.append(it.model_copy(update={
            "item": _normalize_text(it.item, level),
            "evidence_level": level, "source_refs": refs,
        }))

    return CheckReport(
        meta=report.meta, summary=report.summary, doc_check=doc_check,
        risk=risk, project_rules=project_rules, tech_debt=tech_debt,
        manual_checklist=report.manual_checklist, kb_sources=report.kb_sources,
    )
