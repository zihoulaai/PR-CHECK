"""check 拦截闸门（--fail-on）测试。

覆盖：
- parse_gate_rules 合法/非法规则；
- evaluate_gate 对各 section 的命中判定（含 risk 等级阈值）；
- cmd_check 端到端：命中策略返回 EXIT_GATE，未命中返回 EXIT_OK（用 --fake 报告）。
"""
from __future__ import annotations

import argparse
import json

import pytest

from app.agent.gate import evaluate_gate, parse_gate_rules
from app.domain.enums import (
    DocCheckVerdict, EvidenceLevel, RiskLevel, RuleVerdict, TechDebtVerdict,
)
from app.domain.schemas import (
    CheckReport, DocCheckItem, ReportMeta, RiskItem, RuleItem, TechDebtItem,
)


def _report() -> CheckReport:
    return CheckReport(
        meta=ReportMeta(project="team/order"),
        risk=[RiskItem(level=RiskLevel.HIGH, text="高风险改动",
                       evidence_level=EvidenceLevel.C)],
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
                                                      evidence_level=EvidenceLevel.C)]),
        parse_gate_rules(["risk:high"])) == []


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
        "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-    return 1\n+    return 2\n",
        encoding="utf-8",
    )
    return str(p)


def test_check_gate_blocks_on_doc_confirm(tmp_path, capsys):
    from bin.pr_check_cli import EXIT_GATE, EXIT_OK, cmd_check

    rc = cmd_check(_check_ns(_sample_diff(tmp_path), fail_on=["doc:confirm"]))
    assert rc == EXIT_GATE
    err = capsys.readouterr().err
    assert "GATE FAILED" in err


def test_check_gate_blocks_on_risk_medium(tmp_path, capsys):
    from bin.pr_check_cli import EXIT_GATE, cmd_check

    rc = cmd_check(_check_ns(_sample_diff(tmp_path), fail_on=["risk:medium"]))
    assert rc == EXIT_GATE


def test_check_gate_passes_when_no_match(tmp_path):
    from bin.pr_check_cli import EXIT_OK, cmd_check

    rc = cmd_check(_check_ns(_sample_diff(tmp_path), fail_on=["rule:violation"]))
    assert rc == EXIT_OK


def test_check_gate_invalid_spec_raises(tmp_path):
    from bin.pr_check_cli import cmd_check
    from app.errors import ValidationError

    # --fake 报告无 HIGH 风险；risk:extreme 为非法值，应抛校验错误
    with pytest.raises(ValidationError):
        cmd_check(_check_ns(_sample_diff(tmp_path), fail_on=["risk:extreme"]))
