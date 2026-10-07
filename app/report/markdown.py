"""Markdown 渲染：CheckReport JSON → 固定 7 段 Markdown（M4 / PRD §23）。

固定 7 段：
1. 变更摘要  2. 配套文档检查  3. 变更影响与风险  4. 项目规范初检
5. 历史技术债务 / 风险  6. 人工自查  7. 知识库来源
Evidence 以 [A][B][C][N] 徽章显示，来源以 [KB-id] 引用（M4）。
"""
from __future__ import annotations

from app.domain.enums import (
    DocCheckVerdict,
    EvidenceLevel,
    KbStatus,
    RiskLevel,
    RuleVerdict,
    TechDebtVerdict,
)
from app.domain.schemas import CheckReport

_EVIDENCE_BADGE = {
    EvidenceLevel.A: "[A] 已确认",
    EvidenceLevel.B: "[B] 高度相关",
    EvidenceLevel.C: "[C] 建议关注",
    EvidenceLevel.N: "[N] 无法判断",
}

_DOC_VERDICT = {
    DocCheckVerdict.UPDATE: "建议更新",
    DocCheckVerdict.CONFIRM: "建议确认",
    DocCheckVerdict.NO_OBVIOUS_NEED: "无明显需要",
    DocCheckVerdict.UNKNOWN: "无法判断",
}

_RULE_VERDICT = {
    RuleVerdict.OK: "未发现明显问题",
    RuleVerdict.VIOLATION: "发现疑似违反",
    RuleVerdict.UNKNOWN: "无法判断",
}

_DEBT_VERDICT = {
    TechDebtVerdict.DIRECT_MATCH: "直接命中",
    TechDebtVerdict.RELATED: "高度相关",
    TechDebtVerdict.POSSIBLE: "可能存在",
    TechDebtVerdict.NONE_FOUND: "未发现",
    TechDebtVerdict.UNKNOWN: "无法判断",
}

_RISK_LABEL = {
    RiskLevel.HIGH: "高风险",
    RiskLevel.MEDIUM: "中风险",
    RiskLevel.LOW: "低风险",
}


def _refs(refs: list[str]) -> str:
    if not refs:
        return "—"
    return " ".join(f"[{r}]" for r in refs)


def _cell(text: object) -> str:
    """表格单元格消毒：防 LLM 自由文本破坏 Markdown 表格结构。

    - ``|`` 转义为 ``\\|``（否则被当成列分隔符，整行列数错位）；
    - 换行 / 回车压成空格（否则一行截断成多行，表格撕裂）。
    """
    s = "" if text is None else str(text)
    return (s.replace("|", "\\|")
             .replace("\r\n", " ").replace("\r", " ").replace("\n", " "))


def _section_doc_check(report: CheckReport) -> str:
    if not report.doc_check:
        return "本次未发现明显的文档同步需求，或知识库未提供相关规范。"
    rows = ["| 检查项 | 结论 | 依据 | 建议 | 证据 | 来源 |",
            "|---|---|---|---|---|---|"]
    for it in report.doc_check:
        rows.append("| {item} | {verdict} | {basis} | {advice} | {ev} | {refs} |".format(
            item=_cell(it.item), verdict=_DOC_VERDICT.get(it.verdict, it.verdict.value),
            basis=_cell(it.basis) or "—", advice=_cell(it.advice) or "—",
            ev=_EVIDENCE_BADGE.get(it.evidence_level, str(it.evidence_level)),
            refs=_refs(it.source_refs),
        ))
    return "\n".join(rows)


def _section_risk(report: CheckReport) -> str:
    if not report.risk:
        return "未识别到需要重点关注的风险。"
    order = [RiskLevel.HIGH, RiskLevel.MEDIUM, RiskLevel.LOW]
    out = []
    for lvl in order:
        items = [r for r in report.risk if r.level == lvl]
        if not items:
            continue
        out.append(f"### {_RISK_LABEL.get(lvl, lvl.value)}")
        for r in items:
            # location 为「文件:行号」定位指针：有则以行内代码展示，无则省略；
            # 反引号消毒避免破坏 Markdown 行内代码
            loc = f" `{r.location.replace('`', chr(39))}`" if r.location else ""
            out.append(f"-{loc} {r.text}  {_EVIDENCE_BADGE.get(r.evidence_level, '')}  {_refs(r.source_refs)}")
    return "\n".join(out)


def _section_rules(report: CheckReport) -> str:
    if not report.project_rules:
        return "未做项目规范初检（无知识库或未检索到相关规范）。"
    rows = ["| 检查项 | 结论 | 证据 | 来源 |", "|---|---|---|---|"]
    for it in report.project_rules:
        rows.append("| {item} | {verdict} | {ev} | {refs} |".format(
            item=_cell(it.item), verdict=_RULE_VERDICT.get(it.verdict, it.verdict.value),
            ev=_EVIDENCE_BADGE.get(it.evidence_level, ""), refs=_refs(it.source_refs),
        ))
    return "\n".join(rows)


def _section_debt(report: CheckReport) -> str:
    if not report.tech_debt:
        return "未匹配到历史技术债务 / 风险（无知识库或未检索到相关内容）。"
    rows = ["| 检查项 | 结论 | 证据 | 来源 |", "|---|---|---|---|"]
    for it in report.tech_debt:
        rows.append("| {item} | {verdict} | {ev} | {refs} |".format(
            item=_cell(it.item), verdict=_DEBT_VERDICT.get(it.verdict, it.verdict.value),
            ev=_EVIDENCE_BADGE.get(it.evidence_level, ""), refs=_refs(it.source_refs),
        ))
    return "\n".join(rows)


def _section_checklist(report: CheckReport) -> str:
    if not report.manual_checklist:
        return "_（无）_"
    return "\n".join(f"- [ ] {c}" for c in report.manual_checklist)


def _section_sources(report: CheckReport) -> str:
    if report.meta.kb_status == KbStatus.FAILED:
        note = "\n\n> 知识库检索暂时不可用，本次已降级为基础自检。"
    elif report.meta.kb_status == KbStatus.NOT_CONFIGURED:
        note = "\n\n> 知识库未配置，本次为基础自检。"
    else:
        note = ""
    if not report.kb_sources:
        return "本次报告未引用知识库来源。" + note
    lines = [f"{i+1}. [{s.id}] 《{s.title}》（{s.doc_type}，模块 {s.module or '通用'}）"
             for i, s in enumerate(report.kb_sources)]
    return "\n".join(lines) + note


def render_markdown(report: CheckReport) -> str:
    meta_bits = [
        f"分析模式：{report.meta.analysis_mode.value}",
        f"知识库状态：{report.meta.kb_status.value}",
    ]
    if report.meta.cache_hit:
        meta_bits.append("缓存：命中")
    if report.meta.report_id:
        meta_bits.append(f"报告标识：`{report.meta.report_id}`")

    parts = [
        "# PR 提交前置自检报告",
        "",
        "> 本报告用于 PR 提交前辅助自检。不代表正式 Code Review，也不代表代码不存在 Bug。",
        "",
        "> " + " ｜ ".join(meta_bits),
        "",
        "## 1. 变更摘要",
        report.summary or "（无摘要）",
        "",
        "## 2. 配套文档检查",
        _section_doc_check(report),
        "",
        "## 3. 变更影响与风险",
        _section_risk(report),
        "",
        "## 4. 项目规范初检",
        _section_rules(report),
        "",
        "## 5. 历史技术债务 / 风险",
        _section_debt(report),
        "",
        "## 6. 建议人工自查",
        _section_checklist(report),
        "",
        "## 7. 知识库来源",
        _section_sources(report),
    ]
    return "\n".join(parts)
