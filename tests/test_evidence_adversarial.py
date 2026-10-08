"""证据契约的对抗性测试（adversarial FakeLLM）。

此前评估里的「无证据幻觉 / 未知处理」等指标恒真：FakeLLM 默认返回空的
project_rules / tech_debt，而 _assemble 在无 KB 命中时也会清空这两段——两重保证
使任何证据策略回退都测不出来。本文件用**刻意违规**的 LLM 输出 + **非空知识库**
（让 kb_status=success，从而不再被 _assemble 自动清空）驱动全链路，逐条断言
降级行为。一旦 evidence.py 的规则被削弱或删除，这里立刻失败。
"""
from __future__ import annotations

from app.adapters.fakes import FakeKB, FakeLLM
from app.agent.evidence import UNKNOWN_MARKER
from app.domain.enums import EvidenceLevel, RiskLevel, RuleVerdict, TechDebtVerdict

from tests.test_workflow import SMALL_DIFF  # noqa: F401 - 复用同一份小 diff

PROJECT = "team/order"


def _kb_with_real_docs() -> FakeKB:
    """两个类型不同的真实来源：让规则 4（类型契合性）也有用武之地。"""
    kb = FakeKB()
    kb.add_doc(id="kb-debt", title="退款缓存一致性问题", doc_type="technical_debt",
               module="refund", project=PROJECT, snippet="退款缓存需失效策略")
    kb.add_doc(id="kb-rule", title="代码风格与结构规范", doc_type="development_rule",
               module="core", project=PROJECT, snippet="命名与目录约定")
    return kb


def _run(container, llm: FakeLLM):
    container.llm = llm
    container.kb = _kb_with_real_docs()
    from app.agent.workflow import run_check_from_diff

    return run_check_from_diff(SMALL_DIFF, project=PROJECT)


# ===== 前置：这条链路必须真的产出非空知识段落，否则下面全是空断言 =====
def test_adversarial_output_actually_reaches_sections(container):
    """守住测试前提：有 KB 命中时，LLM 的违规内容不会被 _assemble 自动清空。"""
    report = _run(container, FakeLLM(mode="adversarial"))
    assert report.meta.kb_status.value == "success"
    assert report.manual_checklist, "checklist 应来自 LLM（越界长度）"
    # 合规 LLM 走同一路径：project_rules / tech_debt 保持为空，说明清空不是自动的
    ok = _run(container, FakeLLM())
    assert ok.tech_debt == [] and ok.project_rules == []


# ===== 规则 3：伪造引用（真实 id 集合里没有）=====
def test_forged_refs_stripped_and_downgraded(container):
    report = _run(container, FakeLLM(mode="adversarial"))

    dc = report.doc_check[0]
    assert dc.evidence_level == EvidenceLevel.C, "伪造引用不得保留 A 级"
    assert dc.source_refs == [], "伪造 id 必须被剔除"
    assert "已确认" not in dc.basis, "C 级必须弱化强结论词"
    assert "必须" not in dc.advice

    risk = report.risk[0]
    assert risk.evidence_level == EvidenceLevel.C
    assert risk.source_refs == []
    assert risk.location == "src/a.py:1", "location 是定位指针，清洗不得剥离"


def test_forged_refs_keep_matching_secret_words(container):
    """FORBIDDEN_STRONG_WORDS 里的「命中」也必须被弱化——LLM 常写「命中规范」。"""
    report = _run(container, FakeLLM(mode="adversarial"))
    all_text = " ".join(
        [i.basis + i.advice for i in report.doc_check] + [i.text for i in report.risk])
    for w in ("已确认", "必须", "一定", "肯定"):
        assert w not in all_text, w


# ===== 规则 2：无引用却宣称 A/B（LLM 主动承认无证据）→ 丢弃 =====
def test_strong_without_any_ref_is_dropped(container):
    report = _run(container, FakeLLM(mode="adversarial"))
    items = [i.item for i in report.doc_check] + [i.text for i in report.risk]
    assert not any("无证据" in t for t in items), \
        "无 source_refs 的 A 级强结论应被丢弃（规则 2），而非降级保留"


# ===== 规则 5：项目特定强结论（violation / direct_match）必须有 A/B 证据 =====
def test_violation_without_evidence_becomes_unknown_n(container):
    report = _run(container, FakeLLM(mode="adversarial"))
    rule = report.project_rules[0]
    assert rule.verdict == RuleVerdict.UNKNOWN
    assert rule.evidence_level == EvidenceLevel.N
    assert UNKNOWN_MARKER in rule.item, "N 级必须明确「无法判断」"


def test_direct_match_without_evidence_becomes_possible_c(container):
    report = _run(container, FakeLLM(mode="adversarial"))
    debts = {d.item: d for d in report.tech_debt}
    fabricated = debts["伪造证据的历史债务"]
    assert fabricated.verdict == TechDebtVerdict.POSSIBLE, "伪造引用不得保留 direct_match"
    assert fabricated.evidence_level == EvidenceLevel.C
    unsupported = debts["无证据的历史债务"]
    assert unsupported.verdict == TechDebtVerdict.POSSIBLE
    assert unsupported.evidence_level == EvidenceLevel.C


# ===== 规则 7：N 级必须明确「无法判断」=====
def test_n_level_declares_unknown(container):
    report = _run(container, FakeLLM(mode="adversarial"))
    n_items = [r.text for r in report.risk if r.evidence_level == EvidenceLevel.N]
    assert n_items, "应保留一条 N 级风险"
    for text in n_items:
        assert UNKNOWN_MARKER in text, text


# ===== 规则 4：真实命中但类型不支撑的引用 =====
def test_type_mismatched_real_ref_downgraded(container):
    """用《代码风格与结构规范》支撑技术债务判定 → 必须降级。

    真实运行暴露的场景：来源真实命中，规则 1-3 全部放行，但内容与结论无关。
    """
    kb = _kb_with_real_docs()
    container.kb = kb
    container.llm = FakeLLM(report_override={
        "summary": "s", "doc_check": [],
        "risk": [],
        "project_rules": [],
        "tech_debt": [{"item": "Java 正则捕获组错误", "verdict": "direct_match",
                       "evidence_level": "B", "source_refs": ["kb-rule"]}],
        "manual_checklist": [],
    })
    from app.agent.workflow import run_check_from_diff

    report = run_check_from_diff(SMALL_DIFF, project=PROJECT)
    debt = report.tech_debt[0]
    assert debt.evidence_level == EvidenceLevel.C
    assert debt.verdict == TechDebtVerdict.POSSIBLE, \
        "闸门按 verdict 求值：只降等级会让 C 级证据继续阻断 debt:* 闸门"


def test_type_matched_real_ref_keeps_strong(container):
    """类型契合时不得误伤。"""
    container.kb = _kb_with_real_docs()
    container.llm = FakeLLM(report_override={
        "summary": "s", "doc_check": [], "risk": [],
        "project_rules": [],
        "tech_debt": [{"item": "退款缓存未失效", "verdict": "direct_match",
                       "evidence_level": "B", "source_refs": ["kb-debt"]}],
        "manual_checklist": [],
    })
    from app.agent.workflow import run_check_from_diff

    report = run_check_from_diff(SMALL_DIFF, project=PROJECT)
    assert report.tech_debt[0].evidence_level == EvidenceLevel.B
    assert report.tech_debt[0].verdict == TechDebtVerdict.DIRECT_MATCH


# ===== 闸门联动：违规内容不得导致拦截 =====
def test_adversarial_report_does_not_block_gates(container):
    """对抗性输出经过清洗后，不得命中任何 fail-on 闸门。

    这是整套机制的意义所在：不可信的强结论必须失去阻断能力。
    真实运行中曾出现反例：三条 direct_match 引用《代码风格与结构规范》
    （development_rule）支撑技术债务判定，闸门以 EXIT=7 给出错误理由阻断推送。
    """
    from app.agent.gate import evaluate_gate, parse_gate_rules

    report = _run(container, FakeLLM(mode="adversarial"))
    for spec in ("risk:high", "risk:low", "rule:violation", "debt:direct_match"):
        assert evaluate_gate(report, parse_gate_rules([spec])) == [], spec


# ===== 规则 6：C/N 级不得含强结论词 =====
def test_no_strong_words_at_weak_levels(container):
    report = _run(container, FakeLLM(mode="adversarial"))
    weak_texts: list[str] = []
    for it in report.doc_check:
        if it.evidence_level in (EvidenceLevel.C, EvidenceLevel.N):
            weak_texts += [it.basis, it.advice]
    for it in report.risk:
        if it.evidence_level in (EvidenceLevel.C, EvidenceLevel.N):
            weak_texts.append(it.text)
    for it in report.tech_debt:
        if it.evidence_level in (EvidenceLevel.C, EvidenceLevel.N):
            weak_texts.append(it.item)
    for it in report.project_rules:
        weak_texts.append(it.item)
    assert weak_texts
    for w in ("违反", "命中", "已确认", "必须", "一定", "肯定", "明确需要"):
        offenders = [t for t in weak_texts if w in t]
        assert not offenders, f"弱结论等级仍含强结论词「{w}」：{offenders}"


def test_location_survives_sanitization(container):
    """location 是定位指针而非结论，Evidence 清洗不得剥离它。

    被丢弃的 src/b.py:2 属规则 2（无引用的 A 级强结论），因此只有伪造引用那条
    （降级保留）会留下 location。
    """
    report = _run(container, FakeLLM(mode="adversarial"))
    located = {r.location for r in report.risk if r.location}
    assert "src/a.py:1" in located
    assert "src/b.py:2" not in located, "无引用的 A 级条目应整体丢弃"
    assert all(r.level in (RiskLevel.HIGH, RiskLevel.LOW, RiskLevel.MEDIUM) for r in report.risk)


# ===== summary 清洗：此前完全绕过证据规则，是 prompt 注入的落点 =====
def test_poisoned_summary_loses_strong_words(container):
    report = _run(container, FakeLLM(mode="adversarial"))
    for w in ("绝对安全", "已确认", "必须"):
        assert w not in report.summary, report.summary
    assert "建议确认" in report.summary, report.summary


def test_summary_keeps_neutral_statement(container):
    """正常 summary 不得被清洗破坏。"""
    ok = _run(container, FakeLLM())
    assert "离线 Mock" in ok.summary


def test_summary_length_capped(container):
    from app.agent.evidence import _SUMMARY_MAX_CHARS

    container.llm = FakeLLM(report_override={
        "summary": "很长的描述。" * 200, "doc_check": [], "risk": [],
        "project_rules": [], "tech_debt": [], "manual_checklist": [],
    })
    container.kb = _kb_with_real_docs()
    from app.agent.workflow import run_check_from_diff

    report = run_check_from_diff(SMALL_DIFF, project=PROJECT)
    assert len(report.summary) <= _SUMMARY_MAX_CHARS + 1  # +1 为省略号


# ===== 人工清单：限量 + 去重（复读会让清单失去提示价值）=====
def test_checklist_capped_and_deduped(container):
    report = _run(container, FakeLLM(mode="adversarial"))
    assert len(report.manual_checklist) <= 15
    assert len(set(report.manual_checklist)) == len(report.manual_checklist)


def test_checklist_keeps_distinct_items(container):
    container.llm = FakeLLM(report_override={
        "summary": "s", "doc_check": [], "risk": [], "project_rules": [],
        "tech_debt": [],
        "manual_checklist": ["确认文档同步", "确认文档同步", "  ", "确认测试覆盖",
                             "确认异常处理"],
    })
    container.kb = _kb_with_real_docs()
    from app.agent.workflow import run_check_from_diff

    report = run_check_from_diff(SMALL_DIFF, project=PROJECT)
    assert report.manual_checklist == ["确认文档同步", "确认测试覆盖", "确认异常处理"]


# ===== meta 可见性：降级不再静默 =====
def test_degraded_count_reflects_sanitized_items(container):
    """degraded_count = 被证据规则修正的条目数。

    必须对清洗前的报告求值——清洗后已无违规项，对清洗结果统计会恒为 0。
    """
    adversarial = _run(container, FakeLLM(mode="adversarial"))
    # 对抗输出里：伪造引用 3 条 + 无证据强结论 2 条（丢弃）+ violation 1 条
    assert adversarial.meta.degraded_count >= 5, adversarial.meta.degraded_count
    assert adversarial.meta.evidence_issues == [], "默认不输出逐条说明（仅 debug）"


def test_degraded_count_zero_for_compliant_report(container):
    """合规报告不应被计入降级——否则该指标失去信号。"""
    ok = _run(container, FakeLLM())
    assert ok.meta.degraded_count == 0, ok.meta.evidence_issues


def test_evidence_issues_filled_when_debug(container, monkeypatch):
    monkeypatch.setenv("PR_CHECK_DEBUG", "1")
    report = _run(container, FakeLLM(mode="adversarial"))
    assert report.meta.evidence_issues
    joined = " ".join(report.meta.evidence_issues)
    assert "伪造" not in joined or "不存在" in joined  # 说明是可读的中文描述
    assert "[" in joined, "应带段落前缀便于定位"
