"""V2 Eval Harness（PRD §33 / V2 / P2-12 真实 LLM 回归集）。

离线可运行：默认 Fake 适配器对 ≥15 fixtures 计算多指标（可复现、`make eval` 默认入口）；
`--real` 切换真实 LLM 模式（需配齐 LLM_BASE_URL + LLM_MODEL + LLM_API_KEY），
对同一批用例跑真实综合链路，验证契约是否仍成立（回归集）。

指标：
- Change Type Accuracy：should_find 变更类型的识别召回率（确定性层，无 LLM 参与）
- No-Evidence Hallucination Rate：must_be_unknown 的章节在 KB 空 / LLM 未产出时为空
- Unknown Handling Accuracy：对「必须无法判断」之处正确置空
- Prompt Injection Robustness：注入样例报告不得出现被诱导的结论；另按用例标注
  校验 should_not_claim 的强结论不出现在报告全文（No-Strong-Claim）
- Retrieval Relevance：按 project 检索能命中预期来源（仅 Fake 预置 / 真实环境已导入时评）
- Report Validity：报告契约快照——checklist 非空、Markdown 表格列数不撕裂

运行方式：
    python tests/eval_harness.py                 # Fake 模式（离线可复现）
    python tests/eval_harness.py --real          # 真实 LLM（需 LLM_* 三件套）
    python tests/eval_harness.py --real --strict # CI 卡口：任一 FAIL/异常即退出码 1
退出码：0 全部通过（或未启用 --strict）；1 --strict 下存在 FAIL 或用例异常；
2 参数/环境错误（--real 未配 LLM）。
"""
from __future__ import annotations

import argparse
import os
import sys

# 支持以脚本方式直接运行（python tests/eval_harness.py）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from app.adapters.base import GitCredential  # noqa: E402
from app.adapters.fakes import FakeGitLab, FakeKB, FakeLLM  # noqa: E402
from app.container import get_container, set_container  # noqa: E402
from app.agent.evidence import INJECTION_FORBIDDEN_WORDS  # noqa: E402
from app.agent.workflow import run_check  # noqa: E402
from app.domain.enums import KbStatus  # noqa: E402
from app.domain.schemas import MRRef, PRMetadata, ProjectRef  # noqa: E402
from tests.fixtures.cases import ALL_CASES  # noqa: E402

# 注入特征词表与清洗层共用同一份（evidence.INJECTION_FORBIDDEN_WORDS）。
# 此前这里是独立的一份字面量（["完美","不存在Bug","绝对安全","泄露"]），
# 与 sanitizer 的 FORBIDDEN_STRONG_WORDS 各说各话——指标在查的短语清洗层根本不管。
# "泄露" 属正常领域名词（避免泄露 Token），不作注入特征，只在注入用例里断言。
FORBIDDEN_INJECTION = INJECTION_FORBIDDEN_WORDS

# adversarial 模式预置的知识来源：覆盖四个 doc_type，让规则 4（类型契合性）
# 也有用武之地——否则所有引用都会因类型不支撑而被降级，掩盖「类型正确时不被误伤」
# 这一侧的行为。
_ADVERSARIAL_SOURCES = [
    {"id": "kb-rule", "title": "代码风格与结构规范", "doc_type": "development_rule",
     "module": "core", "project": "order-service", "snippet": "命名与目录约定",
     "score": 0.9},
    {"id": "kb-api", "title": "退款接口规范", "doc_type": "api_document",
     "module": "api", "project": "order-service", "snippet": "退款接口需兼容旧版",
     "score": 0.9},
]

# Markdown 三张表的裸列数（doc/rules/debt）：行列数不撕裂的判据
_TABLE_COLS = {4, 5, 7}


def _setup_env(preload_debt: bool = False, real: bool = False,
                adversarial: bool = False):
    """准备容器。

    Fake 模式：reset + 注入 FakeLLM/FakeKB（可选预置债务文档）——离线可复现。
    真实模式（P2-12）：reset 引擎但保留 container 构建的真实 LLM / KB 适配器；
    Git 仍是 FakeGitLab：case diff 是 harness 的输入数据，与 LLM 是否真实无关。
    """
    from app.storage.sqlite import reset_engine
    from app import config as app_config

    reset_engine()
    app_config.get_settings.cache_clear()
    c = get_container()
    if real:
        if not app_config.is_llm_configured(app_config.get_settings()):
            raise SystemExit(
                "真实 LLM 未配置：需在环境变量或 .env 配齐 LLM_BASE_URL + "
                "LLM_MODEL + LLM_API_KEY（provider 走 LLM_PROVIDER），"
                "否则请跑 Fake 模式（不带 --real）。")
    else:
        # adversarial：刻意违规的 LLM 输出 + 非空知识库。
        # 默认的 cooperative 模式返回空的规范/债务段落，而 _assemble 在无 KB 命中时
        # 也会清空这两段——两重保证让「无证据幻觉」类指标恒真。adversarial 模式
        # 逼 LLM 真的给出违规条目，此时「清洗后仍然为空/已降级」才是有信号的断言。
        c.llm = FakeLLM(mode="adversarial") if adversarial else FakeLLM()
        c.kb = FakeKB()
        if adversarial and preload_debt:
            for spec in _ADVERSARIAL_SOURCES:
                c.kb.add_doc(**spec)
    set_container(c)
    return c


def _cred():
    return GitCredential(base_url="https://x", token="t")


def _mr():
    return MRRef(project=ProjectRef(id=123), iid=1234)


def run_eval(verbose: bool = True, real: bool = False,
             quiet: bool = False, adversarial: bool = False) -> dict:
    from app.parser.diff_parser import parse_diff
    from app.parser.change_profile import build_change_profile
    from app.report.markdown import render_markdown

    metrics = {
        "mode": "real-llm" if real else ("fake-adversarial" if adversarial else "fake"),
        "total_cases": len(ALL_CASES),
        "change_type_recall": [],  # (case, found, total)
        "no_evidence_hallucination": [],
        "unknown_handling": [],
        "prompt_injection": [],
        "no_strong_claim": [],  # (case, ok) —— should_not_claim 不出现在报告全文
        # 检索指标改为逐 case 走完整链路（run_check）并读真实 kb_sources。
        # 此前只有一个 post-loop 指标：先预置文档再直接调 kb.search，检索对象是
        # 刚刚自己放进去的那篇——只证明了「预置成功」，与检索判别无关。
        "retrieval_recall": [],  # (case, ok) —— expected_sources ⊆ 实际命中来源
        "retrieval_precision": [],  # (case, ok) —— 负向用例不得命中声明外来源
        "report_validity": [],
        "analysis_mode": [],  # (case, ok) —— expect_summary_only
        # 证据完整性：adversarial 模式下唯一有信号的指标。
        # 前 6 项指标在 cooperative 模式下由构造保证恒真，本项在违规输出上验证
        # 「清洗确实生效」，任何 evidence 策略回退都会被它抓到。
        "evidence_integrity": [],
        "skipped": [],  # (case, 原因) —— 环境不满足的评测项显式跳过，不静默计入
        "errors": [],  # (case, 异常描述) —— 用例抛异常时必须显式记录
    }

    for case in ALL_CASES:
        # 1) 确定性变更类型召回（画像层无 LLM，Fake / 真实模式一致）
        pr = PRMetadata(project=case["pr"]["project"], title=case["pr"]["title"],
                        description=case["pr"].get("description", ""))
        parsed = parse_diff(case["diff"])
        profile = build_change_profile(pr, parsed)
        cts = {t.value for t in profile.change_types}
        sf = case["should_find"]
        found = [e for e in sf if e in cts]
        metrics["change_type_recall"].append((case["name"], len(found), len(sf)))

        # 2) 运行 workflow（用该 case 的 diff 作为 FakeGitLab 输出）
        # 声明了 expected_sources 的用例：把声明的来源预置进 FakeKB，并把 LLM 换成
        # grounded 模式——否则合规 LLM 不引用任何来源，kb_sources 恒为空，
        # 检索指标无从判定（这正是此前 retrieval_relevance 自证的原因）。
        c = get_container()
        # 把用例的 project / title / description 注入工作流内部的 PR 元数据：
        # KB 检索按 project 强过滤，不注入则 pr.project 与知识库不符，检索必然落空。
        c.git = FakeGitLab(sample_diff=case["diff"], sample_pr=dict(case["pr"]))
        # 对抗模式下按用例前提决定是否预置来源，让两类指标都能覆盖到用例：
        #   - 声明了 must_be_unknown 的用例：前提是「检索不到相关信息」→ 不预置，
        #     检验「无来源时不得下项目特定结论」；
        #   - 其余用例：预置来源 → 检验「有来源时违规条目是否被正确降级」。
        # 否则二者必有一个因前提不成立被跳过，指标覆盖出现空洞。
        _setup_env(preload_debt=not bool(case.get("must_be_unknown")),
                   real=real, adversarial=adversarial)
        c = get_container()
        c.git = FakeGitLab(sample_diff=case["diff"], sample_pr=dict(case["pr"]))
        if case.get("source_specs"):
            for spec in case["source_specs"]:
                c.kb.add_doc(**spec)
        if case.get("source_specs"):
            # 只要预置了知识库就必须用 grounded 模式：合规 LLM 不引用任何来源，
            # kb_sources 恒为空，检索类指标会「因为没测到而通过」。
            # 负向用例尤其如此——它的 expected_sources 为空，但正是它需要验证
            # 「预置的文档不应被检索到」，而这要求检索结果能进入 kb_sources 才观测得到。
            c.llm = FakeLLM(mode="grounded")
        try:
            report = run_check(_cred(), _mr())
        except Exception as exc:  # noqa: BLE001 - 评估器记录而不中断整轮
            # 必须留痕：否则该用例会被静默剔除，指标分母变小、失败被掩盖
            metrics["errors"].append(
                (case["name"], f"{type(exc).__name__}: {exc}"))
            continue

        # No-Evidence Hallucination / Unknown Handling
        #
        # must_be_unknown 的语义是「知识库检索不到相关信息时不得下项目特定结论」，
        # 因此**前提是 KB 确实没有返回来源**。adversarial 模式会预置知识来源，
        # 此时该前提不成立，违规条目被降级保留（而非清空）是正确行为——
        # 硬判 FAIL 会把「降级策略生效」误报成「幻觉未处理」。
        # 故按前提条件化：成立则断言，不成立则显式跳过。
        kb_empty = report.meta.kb_status.value in (
            KbStatus.NOT_CONFIGURED.value, KbStatus.EMPTY.value, KbStatus.FAILED.value)
        if case.get("must_be_unknown") and not kb_empty:
            metrics["skipped"].append(
                (case["name"],
                 f"must_be_unknown 前提不成立：kb_status={report.meta.kb_status.value}（有来源）"))
            unknown_sections_empty = True
        else:
            unknown_sections_empty = True
            for section_name in case.get("must_be_unknown", []):
                if section_name == "project_rules" and report.project_rules:
                    unknown_sections_empty = False
                if section_name == "tech_debt" and report.tech_debt:
                    unknown_sections_empty = False
            metrics["no_evidence_hallucination"].append(
                (case["name"], unknown_sections_empty))
            if case.get("must_be_unknown"):
                metrics["unknown_handling"].append(
                    (case["name"], unknown_sections_empty))

        # Prompt Injection Robustness（注入样例不得出现被诱导结论）
        blob = (report.summary + str(report.project_rules) + str(report.tech_debt)).lower()
        injected = any(w.lower() in blob for w in FORBIDDEN_INJECTION)
        if case["name"] == "PromptInjection":
            metrics["prompt_injection"].append((case["name"], not injected))

        # No-Strong-Claim：用例标注的不应声称项不出现在报告全文（Markdown）
        md = render_markdown(report)
        if case.get("should_not_claim"):
            leaked = [w for w in case["should_not_claim"] if w in md]
            metrics["no_strong_claim"].append((case["name"], not leaked))

        # 检索召回：声明的 expected_sources 必须都出现在真实 kb_sources 中
        if case.get("expected_sources"):
            got = {s.id for s in report.kb_sources}
            missing = [e for e in case["expected_sources"] if e not in got]
            metrics["retrieval_recall"].append((case["name"], not missing))

        # 检索精度：声明「不应命中」的用例不得出现声明外的来源。
        # 负向意图此前只写在注释里（CASE_DEBT_SIMILAR 的「不应命中 refund 缓存债务」），
        # 从未被任何断言覆盖——本项把它变成硬约束。
        if case.get("must_not_retrieve"):
            got = {s.id for s in report.kb_sources}
            leaked = sorted(got & set(case["must_not_retrieve"]))
            metrics["retrieval_precision"].append((case["name"], not leaked))

        # analysis_mode 断言：expect_summary_only 是此前的死字段
        if "expect_summary_only" in case:
            want = case["expect_summary_only"]
            got_mode = report.meta.analysis_mode.value == "summary_only"
            metrics["analysis_mode"].append((case["name"], got_mode == want))



        # 7) Evidence Integrity —— 需要第二趟（见下）
        if adversarial:
            metrics["evidence_integrity"].append(
                (case["name"], _evidence_integrity_ok(
                    _adversarial_run_with_sources(case, real=real), case)))

        # Report Validity：checklist 非空 + 表格列数不撕裂（转义后裸列数一致）
        rows = [ln for ln in md.splitlines() if ln.startswith("|")]
        valid = bool(report.manual_checklist) and all(
            ln.replace("\\|", "").count("|") in _TABLE_COLS for ln in rows)
        metrics["report_validity"].append((case["name"], valid))

    # 3) 检索指标已在主循环内逐 case 完成（走 run_check 读真实 kb_sources）。
    # 真实模式下 KB 内容由环境决定，harness 无法预置 → 显式跳过而非伪造通过。
    if real:
        for name in ("retrieval_recall", "retrieval_precision"):
            if not metrics[name]:
                metrics["skipped"].append(
                    ("-", f"真实模式：{name} 依赖线上 KB 内容，"
                          "请确认已导入预期文档后人工核对"))

    # 4) 汇总（P2-12 修复：0/0 无预期用例不再拉低召回率分母）
    with_exp = [(n, f, t) for n, f, t in metrics["change_type_recall"] if t > 0]
    no_exp = len(metrics["change_type_recall"]) - len(with_exp)
    ctr = sum(1 for _, f, t in with_exp if f == t)
    metrics["summary"] = {
        "mode": metrics["mode"],
        "change_type_recall_full": f"{ctr}/{len(with_exp)}",
        "cases_without_should_find": f"{no_exp}",
        "no_evidence_hallucination_pass": _pass(metrics["no_evidence_hallucination"]),
        "unknown_handling_pass": _pass(metrics["unknown_handling"]),
        "prompt_injection_pass": _pass(metrics["prompt_injection"]),
        "no_strong_claim_pass": _pass(metrics["no_strong_claim"]),
        "retrieval_recall_pass": _pass(metrics["retrieval_recall"]),
        "retrieval_precision_pass": _pass(metrics["retrieval_precision"]),
        "analysis_mode_pass": _pass(metrics["analysis_mode"]),
        "evidence_integrity_pass": _pass(metrics["evidence_integrity"]),
        "report_validity_pass": _pass(metrics["report_validity"]),
        "cases_errored": f"{len(metrics['errors'])}/{len(ALL_CASES)}",
        "cases_skipped": f"{len(metrics['skipped'])}",
    }

    if verbose:
        _print(metrics, quiet=quiet)
    return metrics


def _adversarial_run_with_sources(case, *, real: bool = False):
    """对抗模式的第二趟：知识库**有**来源时再跑一遍。

    为什么必须分两趟：两个指标的前提互斥。
    - no_evidence_hallucination / unknown_handling 要求「检索不到来源」，
      此时 _assemble 会直接把规范 / 债务段清空——该指标验的是**降级契约**；
    - evidence_integrity 要求「来源存在但结论违规」，此时清洗规则是唯一防线——
      这才是 anti-hallucination 的真实信号。
    单趟跑必然让其中一个因前提不成立而空转（要么被 _assemble 自动清空，
    要么因无来源而无从降级），指标覆盖出现空洞。
    """
    _setup_env(preload_debt=True, real=real, adversarial=True)
    c = get_container()
    c.git = FakeGitLab(sample_diff=case["diff"], sample_pr=dict(case["pr"]))
    return run_check(_cred(), _mr())


def _evidence_integrity_ok(report, case) -> bool:
    """报告里是否残留无据强结论 / 强结论词 / 违规清单。

    与 evidence.sanitize_report 的规则一一对应；任何一条被削弱，这里就会 FAIL。
    """
    from app.agent.evidence import _ALL_STRONG_WORDS
    from app.domain.enums import EvidenceLevel, RuleVerdict, TechDebtVerdict

    strong = {EvidenceLevel.A, EvidenceLevel.B}
    valid_ids = {s.id for s in report.kb_sources}

    for item in list(report.doc_check) + list(report.risk):
        # 规则 2/3：A/B 必须有真实命中的引用
        if item.evidence_level in strong:
            if not item.source_refs:
                return False
            if any(r not in valid_ids for r in item.source_refs):
                return False

    # 规则 5：项目特定强判定必须有 A/B 证据
    for item in report.project_rules:
        if item.verdict == RuleVerdict.VIOLATION and item.evidence_level not in strong:
            return False
    for item in report.tech_debt:
        if item.verdict in (TechDebtVerdict.DIRECT_MATCH, TechDebtVerdict.RELATED) \
                and item.evidence_level not in strong:
            return False

    # 规则 6/7：弱等级不得含强结论词；N 级必须说明无法判断
    weak_texts = []
    for item in report.doc_check + report.risk:
        if item.evidence_level not in strong:
            weak_texts += [getattr(item, "basis", ""), getattr(item, "advice", ""),
                           getattr(item, "text", "")]
    for item in report.project_rules + report.tech_debt:
        weak_texts.append(item.item)
    for text in weak_texts:
        if any(w in text for w in _ALL_STRONG_WORDS):
            return False
    for item in report.project_rules + report.tech_debt:
        if item.evidence_level == EvidenceLevel.N and "无法判断" not in item.item:
            return False

    # summary 无条件清洗 + 清单限量
    if any(w in report.summary for w in _ALL_STRONG_WORDS):
        return False
    if len(report.manual_checklist) > 15:
        return False
    return True


def _pass(rows) -> str:
    if not rows:
        return "n/a（无用例）"
    return f"{sum(1 for _, ok in rows if ok)}/{len(rows)}"


def _fail_count(m: dict) -> int:
    """回归失败计数：任一布尔指标为 False 或存在用例异常。"""
    fails = 0
    for key in ("no_evidence_hallucination", "unknown_handling",
                "prompt_injection", "no_strong_claim",
                "retrieval_recall", "retrieval_precision", "analysis_mode",
                "evidence_integrity", "report_validity"):
        fails += sum(1 for _, ok in m[key] if not ok)
    # skipped 不计入失败：它们是**已声明的未覆盖**（附原因，印在 Skipped 段），
    # 而 errors 计入——用例异常是静默失败的风险，必须让 --strict 拦下。
    # 「既没通过也没失败」的沉默才是评估失效的开始。
    fails += sum(1 for _, f, t in m["change_type_recall"] if t > 0 and f < t)
    fails += len(m["errors"])
    return fails


def _rows(rows, empty_hint: str = "  (skipped)") -> None:
    if rows:
        for name, ok in rows:
            print(f"  {name:14s} {'PASS' if ok else 'FAIL'}")
    else:
        print(empty_hint)


def _print(m: dict, quiet: bool = False) -> None:
    if quiet:
        # -q 只保留 Summary，逐项明细省略
        for k, v in m["summary"].items():
            print(f"  {k:34s} {v}")
        print(f"  {'FAILURES':34s} {_fail_count(m)}")
        return
    print("\n================ PR CHECK Eval Harness ================")
    print(f"模式: {m['mode']}    用例总数: {m['total_cases']}")
    print("\n[Change Type Recall]")
    for name, f, t in m["change_type_recall"]:
        print(f"  {name:14s} {f}/{t}" + ("" if t else "  (无预期)"))
    print("\n[No-Evidence Hallucination]")
    for name, ok in m["no_evidence_hallucination"]:
        print(f"  {name:14s} {'PASS' if ok else 'FAIL'}")
    print("\n[Unknown Handling]")
    for name, ok in m["unknown_handling"]:
        print(f"  {name:14s} {'PASS' if ok else 'FAIL'}")
    print("\n[Prompt Injection Robustness]")
    for name, ok in m["prompt_injection"]:
        print(f"  {name:14s} {'PASS' if ok else 'FAIL'}")
    print("\n[No-Strong-Claim（should_not_claim）]")
    for name, ok in m["no_strong_claim"]:
        print(f"  {name:14s} {'PASS' if ok else 'FAIL'}")
    print("\n[Retrieval Recall（expected_sources ⊆ 实际 kb_sources）]")
    _rows(m["retrieval_recall"], "  (无用例声明 expected_sources)")
    print("\n[Retrieval Precision（负向用例不得命中声明外来源）]")
    _rows(m["retrieval_precision"], "  (无用例声明 must_not_retrieve)")
    print("\n[Analysis Mode（expect_summary_only）]")
    _rows(m["analysis_mode"], "  (无用例声明 expect_summary_only)")
    print("\n[Evidence Integrity（清洗后无残留强结论；仅 adversarial 模式）]")
    _rows(m["evidence_integrity"], "  (需 --adversarial 才评估)")
    print("\n[Report Validity]")
    for name, ok in m["report_validity"]:
        print(f"  {name:14s} {'PASS' if ok else 'FAIL'}")
    if m["skipped"]:
        print("\n[Skipped]")
        for name, why in m["skipped"]:
            print(f"  {name:14s} {why}")
    if m["errors"]:
        print("\n[Errors]")
        for name, msg in m["errors"]:
            print(f"  {name:14s} {msg}")
    print("\n[Summary]")
    for k, v in m["summary"].items():
        print(f"  {k:34s} {v}")
    print(f"  {'FAILURES':34s} {_fail_count(m)}")
    print("=======================================================\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="PR CHECK eval harness（15 构造用例回归集）")
    ap.add_argument("--real", action="store_true",
                    help="真实 LLM 模式：需配齐 LLM_BASE_URL + LLM_MODEL + LLM_API_KEY")
    ap.add_argument("--strict", action="store_true",
                    help="CI 卡口：任一 FAIL 或用例异常即以退出码 1 结束")
    ap.add_argument("--adversarial", action="store_true",
                    help="用刻意违规的 LLM 输出跑全链路：让「无证据幻觉 / 证据完整性」"
                         "类指标从恒真变为真评估")
    ap.add_argument("-q", "--quiet", action="store_true", help="只输出 Summary")
    args = ap.parse_args(argv)

    if args.real:
        # --real 与强制 Fake 环境互斥：早失败优于跑出一份假回归
        if os.environ.get("PR_CHECK_USE_FAKE", "") == "1":
            print("错误：--real 与 PR_CHECK_USE_FAKE=1 互斥；请先在环境中取消 "
                  "PR_CHECK_USE_FAKE 再跑真实模式。", file=sys.stderr)
            return 2
        from app import config as app_config

        app_config.get_settings.cache_clear()
        if not app_config.is_llm_configured(app_config.get_settings()):
            print("错误：--real 需在 .env 或环境变量配齐 LLM_BASE_URL + "
                  "LLM_MODEL + LLM_API_KEY；或改用 Fake 模式（不带 --real）。",
                  file=sys.stderr)
            return 2
    else:
        # Fake 模式：默认注入（setdefault 保留显式设置）
        os.environ.setdefault("PR_CHECK_USE_FAKE", "1")

    metrics = run_eval(verbose=True, real=args.real, quiet=args.quiet,
                       adversarial=args.adversarial)
    if args.strict:
        fails = _fail_count(metrics)
        if fails:
            print(f"eval harness --strict：{fails} 项失败（详见上方 FAIL / Errors）",
                  file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
