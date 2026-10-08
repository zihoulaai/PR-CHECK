"""check 拦截闸门（gate）：根据报告内容判定是否应阻止提交/推送。

闸门与「基础设施错误」解耦：报告成功生成后，若命中 --fail-on 策略，
check 返回专用退出码 GATE_FAILED（而非 EXIT_OK），git hook 据此中断推送。

证据门槛：risk:* 仅在证据等级为 A/B 时拦截。C 级是「仅凭 Diff / 通用经验推断」，
N 级是「无法判断」——凭推断或凭无知阻断推送没有意义，只会让使用者习惯性
--no-verify。因此未配置知识库（拿不到可溯源来源）时 risk:* 实际不会触发，
这是「无证据不强判」的直接后果。
rule:* 与 debt:* 的强结论本身已由 Evidence 后校验强制要求 A/B 证据；
doc:* 的 confirm/update 本身就是「建议确认」语义，故不额外设门槛。

规则格式（--fail-on 可重复）：<section>:<value>
- risk:high  / risk:medium  / risk:low   （risk 等级 >= 阈值即拦截；阈值越低越严）
- rule:violation                       （命中项目规范违反即拦截）
- doc:confirm / doc:update             （文档待确认/待更新即拦截）
- debt:direct_match / debt:related     （高度相关的技术债务即拦截）
"""
from __future__ import annotations

from app.domain.enums import (
    DocCheckVerdict, EvidenceLevel, RiskLevel, RuleVerdict, TechDebtVerdict,
)
from app.domain.schemas import CheckReport

_RISK_RANK = {RiskLevel.LOW: 1, RiskLevel.MEDIUM: 2, RiskLevel.HIGH: 3}

# 具备可溯源证据的等级；C（纯推断）/ N（无法判断）不作为拦截依据
_BLOCKING_EVIDENCE = {EvidenceLevel.A, EvidenceLevel.B}

_SECTION_VALUES = {
    "risk": {"high", "medium", "low"},
    "rule": {"violation"},
    "doc": {"confirm", "update"},
    "debt": {"direct_match", "related"},
}


def parse_gate_rules(rules: list[str] | None) -> list[tuple[str, str]]:
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
                f"非法的 --fail-on 值：{value!r}"
                f"（段 {section} 可选：{', '.join(sorted(allowed))}）")
        parsed.append((section, value))
    return parsed


def evaluate_gate(report: CheckReport, specs: list[tuple[str, str]]) -> list[str]:
    """返回命中的拦截原因列表（空 = 放行）。specs 为 parse_gate_rules 的输出。"""
    if not specs:
        return []
    violations: list[str] = []
    for section, value in specs:
        if section == "risk":
            threshold = _RISK_RANK[RiskLevel(value)]
            for r in report.risk:
                if r.evidence_level not in _BLOCKING_EVIDENCE:
                    continue  # 无可溯源证据的推断 / 无法判断，不阻断
                if _RISK_RANK[r.level] >= threshold:
                    violations.append(f"风险等级 {r.level.value}：{r.text}")
                    break
        elif section == "rule":
            # 三段的 verdict / 循环变量都分段具名：四个分支共用 `verdict` / `it` 时
            # mypy 会按第一段把它们钉死成 RuleVerdict / RuleItem，后面三段全部报错；
            # 具名化后既过检查，也读得出「这条规则查的是哪一段」。
            rule_verdict = RuleVerdict(value)
            for rule_item in report.project_rules:
                if rule_item.verdict == rule_verdict:
                    violations.append(f"规范违反：{rule_item.item}")
                    break
        elif section == "doc":
            doc_verdict = DocCheckVerdict(value)
            for dc_item in report.doc_check:
                if dc_item.verdict == doc_verdict:
                    violations.append(
                        f"文档待处理：{dc_item.item}（{dc_item.verdict.value}）")
                    break
        elif section == "debt":
            debt_verdict = TechDebtVerdict(value)
            for debt_item in report.tech_debt:
                if debt_item.verdict == debt_verdict:
                    violations.append(
                        f"技术债务命中：{debt_item.item}（{debt_item.verdict.value}）")
                    break
    return violations
