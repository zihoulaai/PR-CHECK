"""R2 反馈与度量：report_id 稳定性、feedback 幂等、metrics 聚合、采集开关。

反馈/闸门事件表不随 conftest 的 KbDoc 清理走，本模块用 autouse fixture 自行隔离。
"""
from __future__ import annotations

import json
import os

import pytest
from sqlmodel import delete

from app.agent.workflow import run_check_from_diff
from app.cli import main
from app.domain.models import AnalysisCache, GateEvent, ReportFeedback
from app.storage.repo import session_scope

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "sample_refund.diff")

SMALL_DIFF = (
    "diff --git a/src/refund/RefundService.java b/src/refund/RefundService.java\n"
    "--- a/src/refund/RefundService.java\n"
    "+++ b/src/refund/RefundService.java\n"
    "@@ -1,2 +1,3 @@\n"
    " package com.refund;\n"
    "+public class RefundService {}\n"
)


@pytest.fixture(autouse=True)
def _clean_store(container):
    """清空 R2/R3 新表，避免共享临时库跨用例串扰。"""
    with session_scope() as s:
        for table in (ReportFeedback, GateEvent, AnalysisCache):
            s.exec(delete(table))
    yield


# ===== report_id =====
def test_report_id_stable_and_diff_sensitive(container):
    """同一变更重复自检得到相同 id；diff 一变即换新 id。"""
    r1 = run_check_from_diff(SMALL_DIFF, project="team/order", source_branch="feat/x")
    r2 = run_check_from_diff(SMALL_DIFF, project="team/order", source_branch="feat/x")
    assert r1.meta.report_id == r2.meta.report_id
    assert r1.meta.report_id.startswith("team-order-feat-x-")

    r3 = run_check_from_diff(SMALL_DIFF + "+// changed\n",
                             project="team/order", source_branch="feat/x")
    assert r3.meta.report_id != r1.meta.report_id


# ===== feedback 幂等 =====
def test_feedback_idempotent_overwrite(container, capsys):
    """同一 (report_id, section, item) 重复标记是覆盖而非追加。"""
    assert main(["feedback", "--report-id", "r1", "--item", "risk:0",
                 "--label", "fp", "--project", "team/order"]) == 0
    capsys.readouterr()
    assert main(["feedback", "--report-id", "r1", "--item", "risk:0",
                 "--label", "useful", "--project", "team/order"]) == 0
    capsys.readouterr()

    assert main(["metrics", "--project", "team/order"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["feedback"]["total"] == 1
    assert out["feedback"]["useful"] == 1
    assert out["feedback"]["false_positive"] == 0
    assert out["feedback"]["by_section"]["risk"] == {"fp": 0, "useful": 1}


def test_feedback_invalid_item_is_invalid_request(container, capsys):
    assert main(["feedback", "--report-id", "r1", "--item", "bogus:0",
                 "--label", "fp"]) == 2
    body = json.loads(capsys.readouterr().out)
    assert body["error"]["code"] == "INVALID_REQUEST"


def test_feedback_disabled_by_env(container, capsys, monkeypatch):
    """PR_CHECK_FEEDBACK=0：feedback 拒绝（rc 2），metrics 仍可读（无数据）。"""
    monkeypatch.setenv("PR_CHECK_FEEDBACK", "0")
    assert main(["feedback", "--report-id", "r1", "--item", "risk:0",
                 "--label", "fp"]) == 2
    capsys.readouterr()
    assert main(["metrics"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["data_available"] is False


# ===== metrics 聚合 =====
def test_metrics_empty_is_not_an_error(container, capsys):
    assert main(["metrics"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["data_available"] is False
    assert "note" in out


def test_metrics_gate_rule_counts_and_rejection(container, capsys):
    """闸门事件被记录；被拦报告收到 fp 反馈即计为该规则的一次「驳回」。"""
    from app.domain.models import GateEvent
    from app.storage.repo import record_gate_event

    record_gate_event(GateEvent(id="g1", report_id="rep-1", project="p",
                                specs="risk:high rule:violation", blocked=True))
    assert main(["feedback", "--report-id", "rep-1", "--item", "risk:0",
                 "--label", "fp", "--project", "p"]) == 0
    capsys.readouterr()

    assert main(["metrics", "--project", "p"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["gate"]["blocked_reports"] == 1
    assert out["gate"]["by_rule"]["risk:high"] == {"hit": 1, "rejected": 1}
    assert out["gate"]["by_rule"]["rule:violation"] == {"hit": 1, "rejected": 1}


def test_check_with_fail_on_records_gate_event(container, capsys):
    """带 --fail-on 的 check 落一条闸门事件（blocked 视证据而定，此处只验记录）。"""
    assert main(["check", "--diff", FIXTURE, "--fake", "--fail-on", "risk:low"]) in (0, 7)
    capsys.readouterr()
    assert main(["metrics"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["gate"]["evaluated_reports"] == 1
    assert "risk:low" in out["gate"]["by_rule"]
