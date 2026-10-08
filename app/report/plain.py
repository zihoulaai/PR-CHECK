"""纯文本渲染：CheckReport JSON → 终端可读的纯文本报告。

与 Markdown 渲染器共享同一套 7 段结构与结论措辞，但输出不含任何 Markdown
语法符号（#、|、**、反引号），未安装 glow / mdcat 等终端渲染器时也能直接阅读。
纯标准库（unicodedata 按终端显示列宽折行），固定宽度，输出稳定可断言。
"""
from __future__ import annotations

import unicodedata

from app.domain.enums import (
    DocCheckVerdict,
    EvidenceLevel,
    KbStatus,
    RiskLevel,
    RuleVerdict,
    TechDebtVerdict,
)
from app.domain.schemas import CheckReport

# 固定折行宽度：比 80 列略宽以容纳「依据/建议」长文，又在多数终端内不折行。
WIDTH = 96
_RULE = "─" * 72

_EVIDENCE_LABEL = {
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
    RuleVerdict.OK: "✓ 未发现明显问题",
    RuleVerdict.VIOLATION: "✗ 发现疑似违反",
    RuleVerdict.UNKNOWN: "? 无法判断",
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


def _flatten(text: object) -> str:
    """压平 LLM 自由文本中的换行，避免一条结论跨段破坏列表缩进。"""
    s = "" if text is None else str(text)
    return " ".join(s.split())


def _cols(ch: str) -> int:
    """字符的终端显示列宽：东亚宽字符（中日韩/全角）占 2 列，其余占 1 列。"""
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _display_width(text: str) -> int:
    return sum(_cols(ch) for ch in text)


def _split_long_word(word: str, limit: int) -> list[str]:
    """把超宽单词（中文无空格长句最常见）按显示列宽逐字切成放得下一行的块。"""
    chunks: list[str] = []
    cur = ""
    used = 0
    for ch in word:
        w = _cols(ch)
        if used and used + w > limit:
            chunks.append(cur)
            cur, used = ch, w
        else:
            cur += ch
            used += w
    chunks.append(cur)
    return chunks


def _wrap(text: str, indent: str = "  ", subsequent: str | None = None,
          width: int = WIDTH) -> list[str]:
    """按终端显示列宽贪心折行：词间空格处换行优先，超宽词（中日韩文）逐字切分。"""
    if not text:
        return []
    sub = subsequent or indent
    lines: list[str] = []
    cur = indent
    limit = width - _display_width(indent)
    for word in text.split(" "):
        # 同一行剩余空间放不下整块时，块本身保证不超过整行容量
        for part in _split_long_word(word, width - _display_width(sub)):
            used = _display_width(cur) - _display_width(indent if not lines else sub)
            w = _display_width(part)
            if used == 0:
                cur += part
            elif used + 1 + w <= limit:
                cur += " " + part
            else:
                lines.append(cur)
                cur = sub + part
                limit = width - _display_width(sub)
    if cur.strip():
        lines.append(cur)
    return lines


def _refs(refs: list[str]) -> str:
    return " ".join(f"[{r}]" for r in refs) if refs else "—"


def _section_title(idx: int, title: str) -> list[str]:
    return [_RULE, f"{idx}. {title}", _RULE]


def _section_doc_check(report: CheckReport) -> list[str]:
    if not report.doc_check:
        return _wrap("本次未发现明显的文档同步需求，或知识库未提供相关规范。")
    out: list[str] = []
    for i, it in enumerate(report.doc_check, 1):
        head = f"{i}. [{_DOC_VERDICT.get(it.verdict, it.verdict.value)}] {_flatten(it.item)}"
        out += _wrap(head, "  ")
        if it.basis:
            out += _wrap("依据：" + _flatten(it.basis), "     ")
        if it.advice:
            out += _wrap("建议：" + _flatten(it.advice), "     ")
        out += _wrap(
            f"证据：{_EVIDENCE_LABEL.get(it.evidence_level, str(it.evidence_level))}"
            f"    来源：{_refs(it.source_refs)}", "     ")
    return out


def _section_risk(report: CheckReport) -> list[str]:
    if not report.risk:
        return _wrap("未识别到需要重点关注的风险。")
    out: list[str] = []
    for lvl in (RiskLevel.HIGH, RiskLevel.MEDIUM, RiskLevel.LOW):
        items = [r for r in report.risk if r.level == lvl]
        if not items:
            continue
        out.append(f"  【{_RISK_LABEL.get(lvl, lvl.value)}】")
        for r in items:
            loc = f"{r.location}  " if r.location else ""
            out += _wrap(f"• {loc}{_flatten(r.text)}", "    ")
            out += _wrap(
                f"证据 {_EVIDENCE_LABEL.get(r.evidence_level, '')}"
                f"    来源 {_refs(r.source_refs)}", "      ")
    return out


def _section_rules(report: CheckReport) -> list[str]:
    if not report.project_rules:
        return _wrap("未做项目规范初检（无知识库或未检索到相关规范）。")
    out: list[str] = []
    for it in report.project_rules:
        verdict = _RULE_VERDICT.get(it.verdict, it.verdict.value)
        out += _wrap(f"{verdict}  {_flatten(it.item)}", "  ")
        out += _wrap(
            f"证据 {_EVIDENCE_LABEL.get(it.evidence_level, '')}"
            f"    来源 {_refs(it.source_refs)}", "      ")
    return out


def _section_debt(report: CheckReport) -> list[str]:
    if not report.tech_debt:
        return _wrap("未匹配到历史技术债务 / 风险（无知识库或未检索到相关内容）。")
    out: list[str] = []
    for it in report.tech_debt:
        verdict = _DEBT_VERDICT.get(it.verdict, it.verdict.value)
        out += _wrap(f"[{verdict}]  {_flatten(it.item)}", "  ")
        out += _wrap(
            f"证据 {_EVIDENCE_LABEL.get(it.evidence_level, '')}"
            f"    来源 {_refs(it.source_refs)}", "      ")
    return out


def _section_checklist(report: CheckReport) -> list[str]:
    if not report.manual_checklist:
        return _wrap("（无）")
    return [line for c in report.manual_checklist
            for line in _wrap(f"[ ] {_flatten(c)}", "  ")]


def _section_sources(report: CheckReport) -> list[str]:
    out: list[str] = []
    if not report.kb_sources:
        out += _wrap("本次报告未引用知识库来源。")
    else:
        for i, s in enumerate(report.kb_sources, 1):
            out += _wrap(
                f"{i}. [{s.id}] 《{s.title}》"
                f"（{s.doc_type or '未分类'}，模块 {s.module or '通用'}）", "  ")
    if report.meta.kb_status == KbStatus.FAILED:
        out += ["", *_wrap("注意：知识库检索暂时不可用，本次已降级为基础自检。")]
    elif report.meta.kb_status == KbStatus.NOT_CONFIGURED:
        out += ["", *_wrap("注意：知识库未配置，本次为基础自检。")]
    elif report.meta.kb_status == KbStatus.NO_DATASET:
        out += ["", *_wrap(
            "注意：本项目未绑定知识库（KB_DATASET_MAP 未命中），本次为基础自检；"
            "闸门依赖 A/B 级证据，在此状态下不会触发。")]
    return out


def render_plain(report: CheckReport) -> str:
    parts: list[str] = [
        "=" * 72,
        "PR 提交前置自检报告",
        "=" * 72,
        f"分析模式：{report.meta.analysis_mode.value}    "
        f"知识库状态：{report.meta.kb_status.value}",
        "本报告用于 PR 提交前辅助自检，不代表正式 Code Review，"
        "也不代表代码不存在 Bug。",
        "",
        *_section_title(1, "变更摘要"),
        *_wrap(report.summary or "（无摘要）"),
        "",
        *_section_title(2, "配套文档检查"),
        *_section_doc_check(report),
        "",
        *_section_title(3, "变更影响与风险"),
        *_section_risk(report),
        "",
        *_section_title(4, "项目规范初检"),
        *_section_rules(report),
        "",
        *_section_title(5, "历史技术债务 / 风险"),
        *_section_debt(report),
        "",
        *_section_title(6, "建议人工自查"),
        *_section_checklist(report),
        "",
        *_section_title(7, "知识库来源"),
        *_section_sources(report),
    ]
    return "\n".join(parts)
