"""check 拦截闸门（gate）：根据报告内容判定是否应阻止提交/推送。

闸门与「基础设施错误」解耦：报告成功生成后，若命中 --fail-on 策略，
check 返回专用退出码 GATE_FAILED（而非 EXIT_OK），git hook 据此中断推送。

规则格式（--fail-on 可重复）：<section>:<value>
- risk:high  / risk:medium / risk:low   （risk 等级 >= 阈值即拦截；high 最严）
- rule:violation                       （命中项目规范违反即拦截）
- doc:confirm / doc:update             （文档待确认/待更新即拦截）
- debt:direct_match / debt:related     （高度相关的技术债务即拦截）
"""
from __future__ import annotations

from app.domain.enums import DocCheckVerdict, RiskLevel, RuleVerdict, TechDebtVerdict
from app.domain.schemas import CheckReport

_RISK_RANK = {RiskLevel.LOW: 1, RiskLevel.MEDIUM: 2, RiskLevel.HIGH: 3}

_SECTION_VALUES = {
    "risk": {"high", "medium", "low"},
    "rule": {"violation"},
    "doc": {"confirm", "update"},
    "debt": {"direct_match", "related"},
}


def parse_gate_rules(rules: "list[str] | None") -> "list[tuple[str, str]]":
    """把 --fail-on 字符串列表解析为 (section, value)；非法格式抛 ValueError。"""
    parsed: list[tuple[str, str]] = []
    for raw in rules or []:
        if ":" not in raw:
            raise ValueError(
                f"非法的 --fail-on 规则：{raw!r}（应为 section:value，"
                f"如 risk:high / rule:violation / doc:confirm）")
        section, value = raw.split(":", 1)
        section = section.strip().lower()
        value = value.strip().lower()
        allowed = _SECTION_VALUES.get(section)
        if allowed is None:
            raise ValueError(
                f"非法的 --fail-on 段：{section!r}（可选：{', '.join(_SECTION_VALUES)}）")
        if value not in allowed:
            raise ValueError(
                f"非法的 --fail-on 值：{value!r}（段 {section} 可选：{', '.join(sorted(allowed))}）")
        parsed.append((section, value))
    return parsed


def evaluate_gate(report: CheckReport, specs: "list[tuple[str, str]]") -> list[str]:
    """返回命中的拦截原因列表（空 = 放行）。specs 为 parse_gate_rules 的输出。"""
    if not specs:
        return []
    violations: list[str] = []
    for section, value in specs:
        if section == "risk":
            threshold = _RISK_RANK[RiskLevel(value)]
            for r in report.risk:
                if _RISK_RANK[r.level] >= threshold:
                    violations.append(f"风险等级 {r.level.value}：{r.text}")
                    break
        elif section == "rule":
            verdict = RuleVerdict(value)
            for it in report.project_rules:
                if it.verdict == verdict:
                    violations.append(f"规范违反：{it.item}")
                    break
        elif section == "doc":
            verdict = DocCheckVerdict(value)
            for it in report.doc_check:
                if it.verdict == verdict:
                    violations.append(f"文档待处理：{it.item}（{it.verdict.value}）")
                    break
        elif section == "debt":
            verdict = TechDebtVerdict(value)
            for it in report.tech_debt:
                if it.verdict == verdict:
                    violations.append(f"技术债务命中：{it.item}（{it.verdict.value}）")
                    break
    return violations
