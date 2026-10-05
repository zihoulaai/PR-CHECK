"""Workflow 降级分支与 summary_only 路径测试（D11 / ADR §4）。"""
from __future__ import annotations

import pytest

from app.adapters.base import GitCredential
from app.adapters.fakes import FakeGitLab
from app.domain.enums import KbStatus
from app.domain.schemas import MRRef, ProjectRef
from app.agent.workflow import run_check
from app.errors import KbError, LlmInvalidOutput
from tests.fixtures.cases import CASE_LARGE


class _FailingKB:
    def search(self, query):
        raise KbError("kb down")

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


def test_llm_failure_raises(container):
    container.git = FakeGitLab()
    container.llm = _FailingLLM()
    with pytest.raises(LlmInvalidOutput):
        run_check(_cred(), _mr())
