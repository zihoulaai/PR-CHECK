"""Workflow 降级分支、summary_only 路径与 LLM 输出契约测试（D11 / ADR §4）。

覆盖：Git 失败即终止、KB 失败降级（含适配器未预料异常）、LLM 失败抛
LlmInvalidOutput、LLM 输出结构不合契约时同样映射 LlmInvalidOutput、
伪造 source_refs 被降级。
"""
from __future__ import annotations

import json

import pytest

from app.adapters.base import GitCredential
from app.adapters.fakes import FakeGitLab, FakeLLM
from app.domain.enums import KbStatus
from app.domain.schemas import MRRef, ProjectRef
from app.agent.workflow import run_check, run_check_from_diff
from app.errors import KbError, LlmInvalidOutput
from tests.fixtures.cases import CASE_LARGE

SMALL_DIFF = (
    "diff --git a/src/refund/RefundController.java b/src/refund/RefundController.java\n"
    "--- a/src/refund/RefundController.java\n"
    "+++ b/src/refund/RefundController.java\n"
    "@@ -1,2 +1,3 @@\n"
    " package com.refund;\n"
    "+@RestController\n"
    " public class RefundController {}\n"
)


class _FailingKB:
    def search(self, query):
        raise KbError("kb down")

    def upload(self, doc):
        return "kb-x"


class _UnexpectedFailKB:
    """适配器抛出非 KbError：降级契约要求仍走 FAILED，而不是冒泡。"""

    def search(self, query):
        raise json.JSONDecodeError("bad body", "x", 0)

    def upload(self, doc):
        return "kb-x"


class _FailingLLM:
    model = "x"

    def complete(self, system, user):
        raise LlmInvalidOutput("bad json")


def _cred():
    return GitCredential(base_url="https://x", token="t")


def _mr():
    return MRRef(project=ProjectRef(id=123), iid=1234)


def test_summary_only_skips_llm_and_kb(container):
    container.git = FakeGitLab(sample_diff=CASE_LARGE["diff"])
    report = run_check(_cred(), _mr())
    assert report.meta.analysis_mode.value == "summary_only"
    assert report.meta.kb_status == KbStatus.NOT_CONFIGURED
    assert report.project_rules == [] and report.tech_debt == []
    assert report.manual_checklist


def test_kb_failure_degrades(container):
    container.git = FakeGitLab()
    container.kb = _FailingKB()
    report = run_check(_cred(), _mr())
    assert report.meta.kb_status == KbStatus.FAILED
    # 降级后仍产出基础自检报告
    assert report.summary


def test_kb_unexpected_exception_still_degrades(container):
    """KB 适配器抛出非 KbError 时也必须降级，而非落到 INTERNAL_ERROR。"""
    container.git = FakeGitLab()
    container.kb = _UnexpectedFailKB()
    report = run_check(_cred(), _mr())
    assert report.meta.kb_status == KbStatus.FAILED
    assert report.summary


def test_llm_failure_raises(container):
    container.git = FakeGitLab()
    container.llm = _FailingLLM()
    with pytest.raises(LlmInvalidOutput):
        run_check(_cred(), _mr())


# ===== LLM 输出结构契约（退出码 5 的来源） =====
@pytest.mark.parametrize("bad_output", [
    pytest.param({"summary": "s", "risk": [{"level": "HIGH", "text": "t"}]},
                 id="枚举大小写错"),
    pytest.param({"summary": "s", "risk": "not-a-list"}, id="字段类型错"),
    pytest.param({"summary": "s", "manual_checklist": [{"a": 1}]}, id="checklist 元素非字符串"),
    pytest.param({"summary": "s", "doc_check": [{"item": "x"}]}, id="条目缺必填字段"),
    pytest.param([1, 2, 3], id="顶层是数组"),
    pytest.param("just a string", id="顶层是字符串"),
])
def test_llm_structurally_invalid_output_raises(container, bad_output):
    """JSON 合法但结构不合约定：必须抛 LlmInvalidOutput（退出码 5），而非 99。"""
    container.llm = FakeLLM(report_override=bad_output)
    with pytest.raises(LlmInvalidOutput):
        run_check_from_diff(SMALL_DIFF, project="team/order")


def test_llm_unknown_fields_ignored(container):
    """LLM 多吐未知字段应被忽略，而非判定为结构错误。"""
    container.llm = FakeLLM(report_override={
        "summary": "s", "risk": [], "doc_check": [], "project_rules": [],
        "tech_debt": [], "manual_checklist": ["a"], "unexpected": {"x": 1},
    })
    report = run_check_from_diff(SMALL_DIFF, project="team/order")
    assert report.manual_checklist == ["a"]


# ===== 伪造 source_refs 的端到端处置 =====
def test_fabricated_source_ref_downgraded(container):
    """LLM 凭空引用不存在的知识库 id：结论降级、ref 剥离、措辞弱化。"""
    from app.adapters.fakes import FakeKB

    kb = FakeKB()
    kb.add_doc(id="kb-real-1", title="退款接口规范", doc_type="api_document",
               module="refund", project="team/order", snippet="退款接口需兼容旧版")
    container.kb = kb
    container.llm = FakeLLM(report_override={
        "summary": "s", "doc_check": [],
        "risk": [{"level": "high", "text": "已确认违反团队规范，必须立即修复",
                  "evidence_level": "A", "source_refs": ["kb-fabricated-999"]}],
        "project_rules": [], "tech_debt": [], "manual_checklist": [],
    })

    report = run_check_from_diff(SMALL_DIFF, project="team/order")
    assert len(report.risk) == 1
    item = report.risk[0]
    assert item.evidence_level.value == "C"
    assert item.source_refs == []
    assert "已确认" not in item.text and "必须" not in item.text
    # 报告第 7 段不会引用不存在的来源
    assert report.kb_sources == []


def test_valid_source_ref_kept_and_listed(container):
    from app.adapters.fakes import FakeKB

    kb = FakeKB()
    kb.add_doc(id="kb-real-1", title="退款接口规范", doc_type="api_document",
               module="refund", project="team/order", snippet="退款接口需兼容旧版")
    container.kb = kb
    container.llm = FakeLLM(report_override={
        "summary": "s", "doc_check": [],
        "risk": [{"level": "high", "text": "退款接口签名变更", "evidence_level": "A",
                  "source_refs": ["kb-real-1"]}],
        "project_rules": [], "tech_debt": [], "manual_checklist": [],
    })

    report = run_check_from_diff(SMALL_DIFF, project="team/order")
    assert report.risk[0].evidence_level.value == "A"
    assert report.risk[0].source_refs == ["kb-real-1"]
    assert [s.id for s in report.kb_sources] == ["kb-real-1"]


def test_n_level_forced_to_unknown_in_report(container):
    container.llm = FakeLLM(report_override={
        "summary": "s", "doc_check": [],
        "risk": [{"level": "low", "text": "已确认违反规范", "evidence_level": "N",
                  "source_refs": []}],
        "project_rules": [], "tech_debt": [], "manual_checklist": [],
    })
    report = run_check_from_diff(SMALL_DIFF, project="team/order")
    assert report.risk[0].text.startswith("无法判断")
