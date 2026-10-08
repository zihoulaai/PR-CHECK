"""Evidence 后校验（M3 / D9）。

强制规则（sanitize_report 中按序执行）：
1. source_refs 必须是本次检索真实命中的知识库 id；无效 id 一律剔除。
2. A / B 级结论若无任何 source_refs（LLM 主动承认无证据），丢弃该条目。
3. A / B 级结论若只引用了不存在的来源（LLM 伪造证据），降级为 C + 剥离 ref + 弱化措辞。
   —— 2 与 3 处置不同：伪造引用通常伴随真实观察，降级保留信息比丢弃更有价值。
4. A / B 级结论的来源类型必须能支撑该段落（见 _ALLOWED_DOC_TYPES）：来源真实命中
   但类型与结论无关（如用《代码风格》支撑「Java 正则捕获组错误」的技术债务）时，
   同样降级为 C。这条堵的是「真实 id + 无关内容」的凑数式引用——规则 1-3 挡不住它。
5. project_rules 的 violation 与 tech_debt 的 direct_match/related 均属项目特定强结论，
   必须有 A/B 证据，否则降级。
6. C / N 级只能弱化表述，禁止强结论词。
7. N 级文本必须明确「无法判断」。

valid_refs 为「本次允许引用的知识库 id 集合」，由 workflow 从 kb_hits 传入；
它与 ref_doc_types 一起构成 keyword-only 参数，强制所有调用方显式声明来源范围，
避免漏改。ref_doc_types 为 {id: doc_type}，规则 4 依赖它；为 None 时跳过规则 4
（仅兼容不提供 doc_type 的旧调用方，生产路径 workflow 始终传入）。
"""
from __future__ import annotations

from app.domain.enums import EvidenceLevel, RuleVerdict, TechDebtVerdict
from app.domain.schemas import CheckReport

# 绝对化断言：这类短语无论有没有证据都不该出现在报告里。
#
# 此前只有评估脚本的 FORBIDDEN_INJECTION 列了它们（完美 / 绝对安全 / 不存在Bug），
# 而 sanitizer 的 FORBIDDEN_STRONG_WORDS 没有——两边清单各说各话，导致
# 「harness 判定为注入特征、但清洗层根本不处理」。收敛到此处作为单一事实来源。
ABSOLUTE_CLAIM_WORDS = [
    "绝对安全", "绝对正确", "完全安全", "完全正确", "绝无风险", "没有问题",
    "完美", "不存在Bug", "不存在 Bug", "不存在缺陷", "不存在问题",
]

FORBIDDEN_STRONG_WORDS = ["违反", "命中", "已确认", "必须", "一定", "肯定", "明确需要"]

# 评估脚本据此判定「注入特征是否出现在报告中」——与清洗层共用同一份清单，
# 避免出现「指标在查、清洗层却不管」的裂缝。
INJECTION_FORBIDDEN_WORDS = ABSOLUTE_CLAIM_WORDS + FORBIDDEN_STRONG_WORDS

# 清洗时实际替换的完整集合（强结论词 + 绝对化断言）
_ALL_STRONG_WORDS = FORBIDDEN_STRONG_WORDS + ABSOLUTE_CLAIM_WORDS

UNKNOWN_MARKER = "无法判断"

# 强结论等级（须有可溯源来源）与弱结论等级（只能弱化表述）
_STRONG_LEVELS = {EvidenceLevel.A, EvidenceLevel.B}
_WEAK_LEVELS = {EvidenceLevel.C, EvidenceLevel.N}

# summary 与人工清单的清洗上限
_SUMMARY_MAX_CHARS = 300
_CHECKLIST_MAX_ITEMS = 15

# 段落 → 可支撑该结论的来源文档类型（规则 4）。
#
# 依据是 kb_query.build_kb_query 的 focus 推导：规范来自 development_rule，
# 接口文档一致性来自 api_document，技术债与历史风险来自 technical_debt /
# historical_risk。因此：
# - project_rules（是否违反项目规范）只能由 development_rule 支撑；
# - doc_check（接口与文档是否一致）只能由 api_document 支撑；
# - tech_debt（是否命中历史债务）由 technical_debt / historical_risk 支撑；
# - risk（工程风险观察）可由债务/风险/规范类文档支撑，但不由 api_document 支撑
#   ——「接口文档没同步」属于 doc_check 的职责，不应作为工程风险的证据来源。
_ALLOWED_DOC_TYPES: dict[str, frozenset[str]] = {
    "doc_check": frozenset({"api_document"}),
    "project_rules": frozenset({"development_rule"}),
    "tech_debt": frozenset({"technical_debt", "historical_risk"}),
    "risk": frozenset({"technical_debt", "historical_risk", "development_rule"}),
}


def _strip_strong_words(text: str) -> str:
    out = text
    for w in _ALL_STRONG_WORDS:
        out = out.replace(w, "建议确认")
    return out


def _normalize_text(text: str, level: EvidenceLevel) -> str:
    """按证据等级规范化文本：C/N 剥离强结论词；N 额外确保出现「无法判断」。"""
    if level not in _WEAK_LEVELS:
        return text
    out = _strip_strong_words(text)
    if level == EvidenceLevel.N and UNKNOWN_MARKER not in out:
        out = f"{UNKNOWN_MARKER}：{out}" if out else UNKNOWN_MARKER
    return out


def _resolve_evidence(item, valid_refs: set[str]) -> tuple[list[str], EvidenceLevel] | None:
    """返回 (有效 refs, 最终证据等级)；None 表示该条目应被丢弃。

    规则 1-3 的唯一实现，四个段落共用，避免各段处置不一致。
    """
    kept = [r for r in item.source_refs if r in valid_refs]
    if item.evidence_level not in _STRONG_LEVELS:
        return kept, item.evidence_level
    if kept:
        return kept, item.evidence_level
    # A/B 但无有效引用：区分「本就无证据」与「引用全部伪造」
    if not item.source_refs:
        return None  # 规则 2：主动承认无证据的强结论，丢弃
    return kept, EvidenceLevel.C  # 规则 3：伪造引用，降级保留


def _enforce_doc_type(section: str, refs: list[str], level: EvidenceLevel,
                      ref_doc_types: dict[str, str] | None) -> tuple[list[str], EvidenceLevel]:
    """规则 4：来源类型必须能支撑该段落，返回 (refs, 最终等级)。

    与规则 1-3 的分工：那几条管「引用是否真实」，本条管「引用是否相关」。
    真实命中却类型不符的来源（用《代码风格》支撑技术债务判定）是凑数式引用，
    伪造 id 拦得住它拦不住——若不降级，闸门会拿一条与结论无关的来源给出阻断理由，
    比不拦截更糟（会训练使用者 --no-verify）。

    处置：
    - C / N 级不受影响（弱结论本来就不依赖来源），refs 原样保留便于追溯。
    - 兼容来源存在 → 剥离不兼容的引用，等级维持（最强证据成立即可）。
    - 全部不兼容（含来源 doc_type 未知/为空）→ 降级为 C，但保留原 refs：
      来源是真实命中的，只是不足以支撑该结论，留给读者判断。
    """
    allowed = _ALLOWED_DOC_TYPES.get(section)
    if ref_doc_types is None or allowed is None or level not in _STRONG_LEVELS:
        return refs, level
    compatible = [r for r in refs if ref_doc_types.get(r, "") in allowed]
    if compatible:
        return compatible, level
    return refs, EvidenceLevel.C


def validate_report(report: CheckReport, *, valid_refs: set[str],
                    ref_doc_types: dict[str, str] | None = None) -> list[str]:
    """返回违规说明列表；空列表表示通过。valid_refs / ref_doc_types 见模块 docstring。"""
    issues: list[str] = []

    def _label(item) -> str:
        return getattr(item, "item", None) or getattr(item, "text", "") or "?"

    def _check(item, section: str) -> None:
        """逐条检查，**每个条目最多产出一条说明**。

        产出一条而非多条的原因：degraded_count 需要等于「被修正的条目数」，
        同一条目同时触发「引用不存在」与「缺有效引用」时不应被计成两次。
        """
        label = _label(item)
        el = item.evidence_level
        problems: list[str] = []

        unknown_refs = [r for r in item.source_refs if r not in valid_refs]
        if unknown_refs:
            problems.append(f"引用了不存在的知识库来源 {unknown_refs}")

        # 规则 1-4 的判定复用 _resolve_evidence / _enforce_doc_type，与 sanitize_report 保持一致
        resolved = _resolve_evidence(item, valid_refs)
        if el in _STRONG_LEVELS and not (resolved[0] if resolved else []):
            problems.append(f"证据等级 {el.value} 缺少有效 source_refs")
        elif resolved is not None:
            typed_refs, typed_level = _enforce_doc_type(
                section, resolved[0], resolved[1], ref_doc_types)
            if typed_level != el:
                problems.append(
                    f"证据等级 {el.value} 的来源类型不支持该段落"
                    f"（可用 {sorted(_ALLOWED_DOC_TYPES.get(section, ()))}，"
                    f"实际 {[(r, ref_doc_types.get(r, '?')) for r in resolved[0]]}）")
            elif set(typed_refs) != set(resolved[0]):
                problems.append(
                    f"含类型不相关的引用 {sorted(set(resolved[0]) - set(typed_refs))}")

        text = " ".join(str(x) for x in (
            getattr(item, "basis", ""), getattr(item, "advice", ""),
            getattr(item, "text", ""),
        ))
        if el in _WEAK_LEVELS:
            hit = [w for w in _ALL_STRONG_WORDS if w in text]
            if hit:
                problems.append(f"为 {el.value} 级却含强结论词 {hit}")
        if el == EvidenceLevel.N and UNKNOWN_MARKER not in text:
            problems.append(f"为 N 级但未明确「{UNKNOWN_MARKER}」")

        if problems:
            issues.append(f"[{section}] {label}：{'；'.join(problems)}")

    for it in report.doc_check:
        _check(it, "doc_check")
    for it in report.risk:
        _check(it, "risk")
    for it in report.project_rules:
        _check(it, "project_rules")
        if it.verdict.value == "violation" and it.evidence_level not in _STRONG_LEVELS:
            issues.append(f"[project_rules] {it.item} 判定为 violation 但证据非 A/B")
    for it in report.tech_debt:
        _check(it, "tech_debt")
        if it.verdict.value in ("direct_match", "related") and not any(
                r in valid_refs for r in it.source_refs):
            issues.append(f"[tech_debt] {it.item} 判定为 {it.verdict.value} 但缺少有效 source_refs")
    return issues


def _sanitize_summary(summary: str) -> str:
    """summary 清洗。

    summary 是唯一被直接打印到终端 / MR 评论的段落，此前**完全绕过**证据清洗：
    doc_check / risk / project_rules / tech_debt 都有强制降级，唯独它与
    manual_checklist 原样透传。而 prompt 注入一旦成功，输出恰恰落在这里。

    它没有可依赖的证据等级（无 source_refs），因此按「严格」策略处理：无条件剥离
    强结论词，并限制长度，避免用长篇叙述淹没其余 6 段。
    """
    out = _strip_strong_words(summary or "").strip()
    if len(out) > _SUMMARY_MAX_CHARS:
        out = out[:_SUMMARY_MAX_CHARS].rstrip() + "…"
    return out


def _sanitize_checklist(items: list[str] | None) -> list[str]:
    """人工清单清洗。

    清单是「需人工确认的事项」，本身不依赖证据等级，故不参与 A/B/C/N 判级；
    但 LLM 复读 / 膨胀会让它失去提示价值（实测见过 40 条近乎重复的清单项，
    以及把测试函数名逐条复述成检查项的退化输出），故限量 + 去重。
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in items or []:
        text = (raw or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= _CHECKLIST_MAX_ITEMS:
            break
    return out


def sanitize_report(report: CheckReport, *, valid_refs: set[str],
                    ref_doc_types: dict[str, str] | None = None) -> CheckReport:
    """修正违反 Evidence 规则的条目：剔除伪造引用 / 丢弃无据强结论 / 降级 / 弱化措辞。

    调用方必须传入本次检索真实命中的 id 集合（valid_refs）；未命中的引用会被剔除，
    从而杜绝 LLM 凭空编造知识库 id 支撑 A/B 级强结论。ref_doc_types 为
    {id: doc_type}，用于规则 4（来源类型须能支撑所在段落）。
    """
    doc_check, risk, project_rules, tech_debt = [], [], [], []

    for it in report.doc_check:
        resolved = _resolve_evidence(it, valid_refs)
        if resolved is None:
            continue
        refs, level = resolved
        refs, level = _enforce_doc_type("doc_check", refs, level, ref_doc_types)
        doc_check.append(it.model_copy(update={
            "basis": _normalize_text(it.basis, level),
            "advice": _normalize_text(it.advice, level),
            "evidence_level": level, "source_refs": refs,
        }))

    for it in report.risk:
        resolved = _resolve_evidence(it, valid_refs)
        if resolved is None:
            continue
        refs, level = resolved
        refs, level = _enforce_doc_type("risk", refs, level, ref_doc_types)
        risk.append(it.model_copy(update={
            "text": _normalize_text(it.text, level),
            "evidence_level": level, "source_refs": refs,
        }))

    for it in report.project_rules:
        resolved = _resolve_evidence(it, valid_refs)
        if resolved is None:
            continue
        refs, level = resolved
        refs, level = _enforce_doc_type("project_rules", refs, level, ref_doc_types)
        # 无证据不得声称违反规范：判定降 unknown、等级降 N。
        # 文本必须按**最终**等级归一化——此前先按降级前的 C 级归一化再改等级，
        # 导致 N 级条目拿不到「无法判断」前缀，违反规则 7（validate_report 能查出
        # 这个不一致，只是它在生产链路里从未被调用）。
        verdict, final_level = it.verdict, level
        if verdict.value == "violation" and level not in _STRONG_LEVELS:
            verdict, final_level = RuleVerdict.UNKNOWN, EvidenceLevel.N
        project_rules.append(it.model_copy(update={
            "item": _normalize_text(it.item, final_level),
            "verdict": verdict,
            "evidence_level": final_level,
            "source_refs": refs,
        }))

    for it in report.tech_debt:
        # 强判定却无任何有效引用（规则 2 主动无证据 / 规则 3 引用无效）：
        # 本段特意**降级保留**而非丢弃——LLM 观察到的风险线索仍有价值。
        if it.verdict.value in ("direct_match", "related") and not any(
                r in valid_refs for r in it.source_refs):
            tech_debt.append(it.model_copy(update={
                "item": _normalize_text(it.item, EvidenceLevel.C),
                "verdict": TechDebtVerdict.POSSIBLE,
                "evidence_level": EvidenceLevel.C,
                "source_refs": [r for r in it.source_refs if r in valid_refs],
            }))
            continue
        resolved = _resolve_evidence(it, valid_refs)
        if resolved is None:
            continue
        refs, level = resolved
        refs, level = _enforce_doc_type("tech_debt", refs, level, ref_doc_types)
        # direct_match / related 本身即项目特定强结论：只有 A/B 证据才成立。
        # 类型不支撑（规则 4）时必须把**判定**一并降为 possible，而不只是降级等级
        # ——闸门按 verdict 求值，只降 level 会让强判定带着 C 级证据继续命中
        # debt:* 闸门，给出与结论无关的阻断理由。
        verdict = it.verdict
        if verdict.value in ("direct_match", "related") and level not in _STRONG_LEVELS:
            verdict = TechDebtVerdict.POSSIBLE
        tech_debt.append(it.model_copy(update={
            "item": _normalize_text(it.item, level),
            "verdict": verdict,
            "evidence_level": level,
            "source_refs": refs,
        }))

    return CheckReport(
        meta=report.meta, summary=_sanitize_summary(report.summary),
        doc_check=doc_check, risk=risk, project_rules=project_rules,
        tech_debt=tech_debt,
        manual_checklist=_sanitize_checklist(report.manual_checklist),
        kb_sources=report.kb_sources,
    )
