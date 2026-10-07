"""R3 LLM 结果缓存：开关、键稳定性、命中标记、缓存不绕过 Evidence、清理与淘汰。"""
from __future__ import annotations

import pytest
from sqlmodel import delete, select

from app.agent.workflow import run_check_from_diff
from app.domain.enums import EvidenceLevel, RiskLevel
from app.domain.models import AnalysisCache
from app.domain.schemas import CheckReport, ReportMeta, RiskItem
from app.storage.cache import cache_enabled, make_cache_key, put_cached
from app.storage.repo import clear_cache, prune_cache, session_scope

SMALL_DIFF = (
    "diff --git a/src/pay/PayService.java b/src/pay/PayService.java\n"
    "--- a/src/pay/PayService.java\n"
    "+++ b/src/pay/PayService.java\n"
    "@@ -1,2 +1,3 @@\n"
    " package com.pay;\n"
    "+public class PayService {}\n"
)


@pytest.fixture(autouse=True)
def _clean_cache(container):
    with session_scope() as s:
        s.exec(delete(AnalysisCache))
    yield


def _mini_report() -> CheckReport:
    return CheckReport(meta=ReportMeta(project="p"))


def test_cache_disabled_by_default(container):
    assert cache_enabled() is False


def test_cache_key_ignores_kb_id_order_and_reacts_to_components(container):
    k = make_cache_key("diff", "m", {"b", "a"})
    assert k == make_cache_key("diff", "m", {"a", "b"})
    assert make_cache_key("diff2", "m", {"a"}) != k
    assert make_cache_key("diff", "m2", {"a"}) != k
    assert make_cache_key("diff", "m", {"a", "c"}) != k


def test_cache_hit_on_second_run(container, monkeypatch):
    monkeypatch.setenv("PR_CHECK_CACHE", "1")
    r1 = run_check_from_diff(SMALL_DIFF, project="p", source_branch="s")
    assert r1.meta.cache_hit is False
    r2 = run_check_from_diff(SMALL_DIFF, project="p", source_branch="s")
    assert r2.meta.cache_hit is True


def test_cache_not_used_when_disabled(container):
    r1 = run_check_from_diff(SMALL_DIFF, project="p", source_branch="s")
    r2 = run_check_from_diff(SMALL_DIFF, project="p", source_branch="s")
    assert r1.meta.cache_hit is False and r2.meta.cache_hit is False


def test_cached_report_still_passes_evidence_sanitize(container, monkeypatch):
    """缓存命中仍走 sanitize_report：伪造 refs 的 A 级强结论必须被降级。"""
    monkeypatch.setenv("PR_CHECK_CACHE", "1")
    run_check_from_diff(SMALL_DIFF, project="p", source_branch="s")  # 写入缓存

    forged = CheckReport(
        meta=ReportMeta(project="p"),
        risk=[RiskItem(level=RiskLevel.HIGH, text="必须是漏洞",
                       evidence_level=EvidenceLevel.A, source_refs=["kb-fake"])],
    )
    with session_scope() as s:
        entry = list(s.exec(select(AnalysisCache)).all())[0]
        entry.report_json = forged.model_dump_json()
        s.add(entry)

    r = run_check_from_diff(SMALL_DIFF, project="p", source_branch="s")
    assert r.meta.cache_hit is True
    # 有效 refs 为空 → A 级伪造引用被降级为 C，不再以 [A] 出现
    assert r.risk and all(i.evidence_level == EvidenceLevel.C for i in r.risk)


def test_cache_clear_and_prune(container):
    for i in range(3):
        put_cached(make_cache_key(f"d{i}", "m", set()), _mini_report(), "m", "p")
    with session_scope() as s:
        assert len(list(s.exec(select(AnalysisCache)).all())) == 3
    assert prune_cache(2) == 1
    assert clear_cache("p") == 2