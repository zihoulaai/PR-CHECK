"""Workflow 降级分支、summary_only 路径与 LLM 输出契约测试（D11 / ADR §4）。

覆盖：Git 失败即终止、KB 失败降级（含适配器未预料异常）、LLM 失败抛
LlmInvalidOutput、LLM 输出结构不合契约时同样映射 LlmInvalidOutput、
伪造 source_refs 被降级、--prune 过期来源检索过滤。
"""
from __future__ import annotations

import json

import pytest

from app.adapters.base import GitCredential
from app.adapters.fakes import FakeGitLab, FakeLLM
from app.domain.enums import KbStatus
from app.domain.schemas import KBHit, MRRef, ProjectRef
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
    """真实命中的来源应保留 A 级并进入 kb_sources。

    来源类型必须与所在段落契合（evidence 规则 4）：risk 只接受
    technical_debt / historical_risk / development_rule，因此这里用
    historical_risk；早前用 api_document 支撑风险条目属于错配式引用，
    正是该规则要拦的情况。
    """
    from app.adapters.fakes import FakeKB

    kb = FakeKB()
    kb.add_doc(id="kb-real-1", title="退款缓存历史风险", doc_type="historical_risk",
               module="refund", project="team/order", snippet="退款缓存需失效策略")
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


def test_wrong_doc_type_source_is_downgraded(container):
    """真实命中但类型不支撑该段落的来源必须降级（evidence 规则 4）。

    真实运行暴露的场景：tech_debt 的 direct_match 引用《代码风格与结构规范》
    （development_rule）——来源真实存在，但与结论无关。伪造 id 挡得住，
    这种「真实 id + 无关内容」的凑数引用挡不住。
    """
    from app.adapters.fakes import FakeKB

    kb = FakeKB()
    kb.add_doc(id="kb-style", title="代码风格与结构规范", doc_type="development_rule",
               module="core", project="team/order", snippet="命名与目录约定")
    container.kb = kb
    container.llm = FakeLLM(report_override={
        "summary": "s", "doc_check": [],
        "risk": [],
        "project_rules": [],
        "tech_debt": [{"item": "Java 正则捕获组错误", "verdict": "direct_match",
                       "evidence_level": "B", "source_refs": ["kb-style"]}],
        "manual_checklist": [],
    })

    report = run_check_from_diff(SMALL_DIFF, project="team/order")
    debt = report.tech_debt[0]
    assert debt.evidence_level.value == "C"
    assert debt.verdict.value == "possible"


def test_n_level_forced_to_unknown_in_report(container):
    container.llm = FakeLLM(report_override={
        "summary": "s", "doc_check": [],
        "risk": [{"level": "low", "text": "已确认违反规范", "evidence_level": "N",
                  "source_refs": []}],
        "project_rules": [], "tech_debt": [], "manual_checklist": [],
    })
    report = run_check_from_diff(SMALL_DIFF, project="team/order")
    assert report.risk[0].text.startswith("无法判断")


# ===== kb import --prune 产物的检索过滤（P2-9） =====
def _hit(doc_id: str) -> KBHit:
    return KBHit(id=doc_id, title=f"文档 {doc_id}", doc_type="api_document")


def test_drop_stale_filters_marked_docs(container):
    """stale 标记的来源被剔除，active 来源保留。"""
    from app.agent.workflow import _drop_stale
    from app.domain.models import KbDoc
    from app.storage.repo import insert_kb_doc

    insert_kb_doc(KbDoc(id="kb-stale-1", project="team/order", module="refund",
                        doc_type="api_document", title="过期规范", status="stale"))
    hits = [_hit("kb-stale-1"), _hit("kb-live-1")]
    kept = _drop_stale(hits, "team/order")
    assert [h.id for h in kept] == ["kb-live-1"]
    # 空命中直接返回，不查库
    assert _drop_stale([], "team/order") == []


def test_drop_stale_scoped_by_project(container):
    """stale 标记按 project 隔离：其他项目的过期标记不影响本项目检索。"""
    from app.agent.workflow import _drop_stale
    from app.domain.models import KbDoc
    from app.storage.repo import insert_kb_doc

    insert_kb_doc(KbDoc(id="kb-stale-1", project="other/proj",
                        doc_type="api_document", title="他项目过期", status="stale"))
    hits = [_hit("kb-stale-1")]
    assert [h.id for h in _drop_stale(hits, "team/order")] == ["kb-stale-1"]


def test_drop_stale_skips_filter_on_db_error(container, monkeypatch):
    """DB 查询异常时放行检索结果：检索结果优先于本地元数据。"""
    import app.storage.repo as repo
    from app.agent.workflow import _drop_stale

    def boom(*_a, **_kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(repo, "list_stale_doc_ids", boom)
    hits = [_hit("kb-1")]
    assert _drop_stale(hits, "team/order") == hits


def test_stale_source_excluded_end_to_end(container):
    """端到端：被 --prune 标记 stale 的来源即使检索命中也不进报告且不充当证据。"""
    from app.adapters.fakes import FakeKB
    from app.domain.enums import KbStatus
    from app.domain.models import KbDoc
    from app.storage.repo import insert_kb_doc

    kb = FakeKB()
    kb.add_doc(id="kb-stale-1", title="过期接口规范", doc_type="api_document",
               module="refund", project="team/order", snippet="旧版退款契约")
    container.kb = kb
    insert_kb_doc(KbDoc(id="kb-stale-1", project="team/order", module="refund",
                        doc_type="api_document", title="过期接口规范", status="stale"))
    container.llm = FakeLLM(report_override={
        "summary": "s", "doc_check": [],
        "risk": [{"level": "high", "text": "接口签名变更", "evidence_level": "A",
                  "source_refs": ["kb-stale-1"]}],
        "project_rules": [], "tech_debt": [], "manual_checklist": [],
    })
    report = run_check_from_diff(SMALL_DIFF, project="team/order")
    # 命中被剔除 → kb_status=empty → 「无知识不强判」，ref 失效证据降级
    assert report.meta.kb_status == KbStatus.EMPTY
    assert report.risk[0].evidence_level.value == "C"
    assert report.risk[0].source_refs == []
    assert report.kb_sources == []


# ===== checklist 与变更画像联动（P2-10） =====
def _profile(**kwargs):
    from app.domain.schemas import ChangeProfile

    return ChangeProfile(**kwargs)


def test_checklist_base_items_always_present():
    """无风险主题的画像（纯注释/测试变更）也要有基础项，清单不落空。"""
    from app.agent.workflow import build_checklist
    from app.domain.enums import ChangeType

    items = build_checklist(_profile(change_types=[ChangeType.COMMENT_CHANGE]))
    assert items == ["测试覆盖", "异常和边界条件", "文档同步"]


def test_checklist_triggered_by_change_type():
    """DATABASE_CHANGE 命中数据库主题：基础项 + 专项项。"""
    from app.agent.workflow import build_checklist
    from app.domain.enums import ChangeType

    items = build_checklist(_profile(change_types=[ChangeType.DATABASE_CHANGE]))
    assert "数据库迁移与回滚脚本（索引、锁、数据量评估）" in items
    assert items[:3] == ["测试覆盖", "异常和边界条件", "文档同步"]


def test_checklist_triggered_by_high_impact_feature():
    """HighImpactFeature 同样触发专项项（与类型表互补，如并发）。"""
    from app.agent.workflow import build_checklist
    from app.domain.enums import HighImpactFeature

    items = build_checklist(_profile(
        high_impact_features=[HighImpactFeature.CONCURRENCY]))
    assert "并发安全（竞态、死锁）" in items


def test_checklist_dedup_same_topic():
    """同主题的 ChangeType 与 HighImpactFeature 共享文本，只出一项。"""
    from app.agent.workflow import build_checklist
    from app.domain.enums import ChangeType, HighImpactFeature

    items = build_checklist(_profile(
        change_types=[ChangeType.DATABASE_CHANGE],
        high_impact_features=[HighImpactFeature.DATABASE],
    ))
    db_text = "数据库迁移与回滚脚本（索引、锁、数据量评估）"
    assert items.count(db_text) == 1
    # 无重复项
    assert len(items) == len(set(items))


def test_checklist_file_ops_appended():
    """新增/删除/重命名文件追加对应确认项。"""
    from app.agent.workflow import build_checklist

    items = build_checklist(_profile(added_files=1, deleted_files=2, renamed_files=1))
    assert "新增文件是否纳入构建/发布/忽略规则" in items
    assert "删除文件的影响面（残留引用是否清理干净）" in items
    assert "重命名可追溯性（git 是否识别为 rename、引用是否同步）" in items


def test_checklist_order_is_stable():
    """输出顺序只取决于映射表声明顺序，与画像中触发器的出现顺序无关。"""
    from app.agent.workflow import build_checklist
    from app.domain.enums import ChangeType, HighImpactFeature

    a = build_checklist(_profile(
        change_types=[ChangeType.AUTH_CHANGE, ChangeType.CACHE_CHANGE],
        high_impact_features=[HighImpactFeature.CONCURRENCY],
    ))
    b = build_checklist(_profile(
        change_types=[ChangeType.CACHE_CHANGE, ChangeType.AUTH_CHANGE],
        high_impact_features=[HighImpactFeature.CONCURRENCY],
    ))
    assert a == b


def test_summary_only_checklist_matches_profile(container):
    """summary_only 报告的 checklist 来自画像联动（非固定列表）。"""
    from app.agent.workflow import _build_summary_only
    from app.domain.enums import (AnalysisMode, ChangeType, HighImpactFeature,
                                  KbStatus)
    from app.domain.schemas import ChangeProfile, PRMetadata

    pr = PRMetadata(project="team/order", repository="team/order", pr_id=0, title="t")
    profile = ChangeProfile(
        changed_files=1, change_types=[ChangeType.DATABASE_CHANGE],
        high_impact_features=[HighImpactFeature.PUBLIC_API], added_files=1,
    )
    report = _build_summary_only(pr, profile, KbStatus.NOT_CONFIGURED,
                                 mode=AnalysisMode.SUMMARY_ONLY)
    assert "数据库迁移与回滚脚本（索引、锁、数据量评估）" in report.manual_checklist
    assert "API 兼容性（调用方是否需要同步改造、契约是否版本化）" in report.manual_checklist
    assert "新增文件是否纳入构建/发布/忽略规则" in report.manual_checklist


def test_llm_empty_checklist_falls_back_to_profile(container):
    """LLM 输出空 checklist 时回落画像联动版；非空时仍以 LLM 为准。"""
    from app.agent.workflow import run_check_from_diff

    container.llm = FakeLLM(report_override={
        "summary": "s", "doc_check": [], "risk": [],
        "project_rules": [], "tech_debt": [], "manual_checklist": [],
    })
    report = run_check_from_diff(SMALL_DIFF, project="team/order")
    # 回落值为画像联动：非空、含基础项
    assert report.manual_checklist
    assert report.manual_checklist[:3] == ["测试覆盖", "异常和边界条件", "文档同步"]
