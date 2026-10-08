"""check 拦截闸门（--fail-on）测试。

覆盖：
- parse_gate_rules 合法/非法规则；
- evaluate_gate 对各 section 的命中判定（含 risk 等级阈值与证据门槛）；
- cmd_check 端到端：命中策略返回 EXIT_GATE，未命中返回 EXIT_OK（用 --fake 报告）。
"""
from __future__ import annotations

import argparse

import pytest

from app.agent.gate import evaluate_gate, parse_gate_rules
from app.domain.enums import (
    DocCheckVerdict, EvidenceLevel, RiskLevel, RuleVerdict, TechDebtVerdict,
)
from app.domain.schemas import (
    CheckReport, DocCheckItem, ReportMeta, RiskItem, RuleItem, TechDebtItem,
)

# 可溯源证据的引用（闸门只认 A/B 级）
_REF = "kb-real-1"


def _report() -> CheckReport:
    return CheckReport(
        meta=ReportMeta(project="team/order"),
        risk=[RiskItem(level=RiskLevel.HIGH, text="高风险改动",
                       evidence_level=EvidenceLevel.A, source_refs=[_REF])],
        project_rules=[RuleItem(item="命名规范", verdict=RuleVerdict.VIOLATION,
                               evidence_level=EvidenceLevel.C)],
        doc_check=[DocCheckItem(item="API文档", verdict=DocCheckVerdict.CONFIRM,
                                evidence_level=EvidenceLevel.C)],
        tech_debt=[TechDebtItem(item="legacy模块", verdict=TechDebtVerdict.DIRECT_MATCH,
                                evidence_level=EvidenceLevel.C)],
    )


def test_parse_gate_rules_valid():
    specs = parse_gate_rules(["risk:high", "rule:violation", "doc:confirm", "debt:related"])
    assert specs == [("risk", "high"), ("rule", "violation"),
                     ("doc", "confirm"), ("debt", "related")]


def test_parse_gate_rules_invalid_section():
    with pytest.raises(ValueError):
        parse_gate_rules(["bogus:x"])


def test_parse_gate_rules_invalid_value():
    with pytest.raises(ValueError):
        parse_gate_rules(["risk:extreme"])


def test_parse_gate_rules_missing_colon():
    with pytest.raises(ValueError):
        parse_gate_rules(["rule-violation"])


def test_evaluate_gate_all_sections():
    v = evaluate_gate(_report(), parse_gate_rules(
        ["risk:high", "rule:violation", "doc:confirm", "debt:direct_match"]))
    assert len(v) == 4


def test_evaluate_gate_risk_threshold():
    # risk:low 阈值下，HIGH 风险命中
    assert evaluate_gate(_report(), parse_gate_rules(["risk:low"]))
    # risk:high 仅 HIGH 命中；MEDIUM 不命中
    assert evaluate_gate(
        CheckReport(meta=ReportMeta(), risk=[RiskItem(level=RiskLevel.MEDIUM, text="m",
                                                      evidence_level=EvidenceLevel.A,
                                                      source_refs=[_REF])]),
        parse_gate_rules(["risk:high"])) == []


@pytest.mark.parametrize("level", [EvidenceLevel.C, EvidenceLevel.N])
def test_risk_without_traceable_evidence_never_blocks(level):
    """无据推断（C）或无法判断（N）不阻断推送：否则只能训练使用者 --no-verify。"""
    report = CheckReport(meta=ReportMeta(), risk=[
        RiskItem(level=RiskLevel.HIGH, text="疑似高风险", evidence_level=level),
    ])
    for spec in ("risk:low", "risk:medium", "risk:high"):
        assert evaluate_gate(report, parse_gate_rules([spec])) == [], spec


def test_risk_with_fabricated_ref_never_blocks():
    """引用了不存在来源的 A 级风险会被 Evidence 降级，闸门自然不拦。"""
    report = CheckReport(meta=ReportMeta(), risk=[
        RiskItem(level=RiskLevel.HIGH, text="无据高风险", evidence_level=EvidenceLevel.C,
                 source_refs=["kb-fabricated"]),
    ])
    assert evaluate_gate(report, parse_gate_rules(["risk:high"])) == []


def test_evaluate_gate_rule_no_match():
    # 报告无 VIOLATION（project_rules 为空），rule:violation 不命中
    report = _report()
    report.project_rules = []
    assert evaluate_gate(report, parse_gate_rules(["rule:violation"])) == []


def test_evaluate_gate_empty_rules_passes():
    assert evaluate_gate(_report(), []) == []


def _check_ns(diff_file: str, **over) -> argparse.Namespace:
    base = dict(
        fake=True, project=None, repo=None, base=None, source=None,
        diff=diff_file, input=None, title="", description="", source_branch="",
        target_branch="", author="", format="json", pretty=False, fail_on=None,
    )
    base.update(over)
    return argparse.Namespace(**base)


def _sample_diff(tmp_path: str) -> str:
    from pathlib import Path
    p = Path(tmp_path) / "pr.diff"
    p.write_text(
        "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n"
        "@@ -1 +1 @@\n-    return 1\n+    return 2\n",
        encoding="utf-8",
    )
    return str(p)


def test_check_gate_blocks_on_doc_confirm(tmp_path, capsys):
    from app.cli import EXIT_GATE, cmd_check

    rc = cmd_check(_check_ns(_sample_diff(tmp_path), fail_on=["doc:confirm"]))
    assert rc == EXIT_GATE
    err = capsys.readouterr().err
    assert "GATE FAILED" in err


def test_check_gate_blocks_on_risk_medium(container, tmp_path, capsys):
    """risk:* 需要可溯源证据：LLM 引用真实命中的知识库来源时才会拦截。"""
    from app.adapters.fakes import FakeKB, FakeLLM
    from app.cli import EXIT_GATE, cmd_check

    kb = FakeKB()
    kb.add_doc(id="kb-real-1", title="退款接口规范", doc_type="development_rule",
               module="refund", project="team/order", snippet="退款接口需兼容旧版")
    container.kb = kb
    container.llm = FakeLLM(report_override={
        "summary": "s", "doc_check": [],
        "risk": [{"level": "medium", "text": "退款接口签名变更",
                  "evidence_level": "A", "source_refs": ["kb-real-1"]}],
        "project_rules": [], "tech_debt": [], "manual_checklist": [],
    })
    rc = cmd_check(_check_ns(_sample_diff(tmp_path), project="team/order",
                             fake=False,  # 保留注入的容器，不让 --fake 重建
                             fail_on=["risk:medium"]))
    assert rc == EXIT_GATE


def test_check_gate_does_not_block_on_unevidenced_risk(container, tmp_path):
    """C 级风险（FakeLLM 默认输出）不再触发 risk 闸门。"""
    from app.cli import EXIT_OK, cmd_check

    rc = cmd_check(_check_ns(_sample_diff(tmp_path), fail_on=["risk:medium"]))
    assert rc == EXIT_OK


def test_check_gate_passes_when_no_match(tmp_path):
    from app.cli import EXIT_OK, cmd_check

    rc = cmd_check(_check_ns(_sample_diff(tmp_path), fail_on=["rule:violation"]))
    assert rc == EXIT_OK


def test_check_gate_invalid_spec_raises(tmp_path):
    from app.cli import cmd_check
    from app.errors import ValidationError

    # --fake 报告无 HIGH 风险；risk:extreme 为非法值，应抛校验错误
    with pytest.raises(ValidationError):
        cmd_check(_check_ns(_sample_diff(tmp_path), fail_on=["risk:extreme"]))
