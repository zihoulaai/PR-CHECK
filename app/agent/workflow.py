"""Agent 线性工作流（ADR §2 / PRD §4 / D11）。

流程：归一引用 → Git 获取（失败即终止）→ Diff 解析 → ChangeProfile →
（KB 单次检索，失败降级）→ LLM 综合（失败整体失败）→ Evidence 校验 → CheckReport。
无循环、无 Reflection、无多轮检索；Git 是数据源非 Agent 工具（Agent 唯一工具为 KB 检索）。

run_check 与 run_check_from_diff 仅负责取 diff / 组装元数据，
共享 _analyze 承载全部降级策略，避免两处逻辑分叉。
"""
from __future__ import annotations

import json
import logging

from app.adapters.base import GitCredential
from app.agent.evidence import sanitize_report
from app.agent.kb_query import build_kb_query
from app.agent.prompt import build_system_prompt, build_user_prompt
from app.container import get_container, select_git_adapter
from app.domain.enums import AnalysisMode, EvidenceLevel, KbStatus, RiskLevel
from app.domain.schemas import (
    CheckReport, KbSource, MRRef, PRMetadata, ReportMeta, ReportSections, RiskItem,
)
from app.errors import KbError, LlmInvalidOutput
from app.parser.change_profile import build_change_profile, select_focused_diff
from app.parser.diff_parser import ParsedFile, parse_diff
from pydantic import ValidationError as PydanticValidationError

logger = logging.getLogger("pr_check")


def run_check(cred: GitCredential, mr_ref: MRRef) -> CheckReport:
    container = get_container()
    # 选择本地 Git 适配器（直连 .git，无需 Token）
    git = select_git_adapter(cred.platform)

    # 1) Git 获取（失败即终止，不进入 Agent）
    pr: PRMetadata = git.get_mr(cred, mr_ref)
    diff_text: str = git.get_diff(cred, mr_ref)

    # 2) Diff 解析 + 变更画像 → 3~5 步由 _analyze 统一承担
    parsed = parse_diff(diff_text)
    profile = build_change_profile(pr, parsed)
    return _analyze(container, pr, profile, parsed, diff_text)


def run_check_from_diff(
    diff_text: str,
    *,
    project: str = "",
    title: str = "",
    description: str = "",
    source_branch: str = "",
    target_branch: str = "",
    author: str = "",
) -> CheckReport:
    """不依赖 GitLab：直接对 diff 文本跑完整自检。

    链路与 run_check 完全一致（parse_diff → build_change_profile → KB 检索 →
    LLM 综合 → sanitize_report），二者共用 _analyze。当 LLM 未配置时降级为
    summary_only 基础报告，保证离线（如 CLI --fake 无真实 LLM）也能产出可用结果。
    """
    pr = PRMetadata(
        project=project, repository=project, pr_id=0, title=title,
        description=description, source_branch=source_branch,
        target_branch=target_branch, author=author,
    )
    parsed = parse_diff(diff_text)
    profile = build_change_profile(pr, parsed)
    return _analyze(get_container(), pr, profile, parsed, diff_text)


def _analyze(container, pr: PRMetadata, profile, parsed: list[ParsedFile],
             diff_text: str) -> CheckReport:
    """KB 检索 → LLM 综合 → Evidence 校验 → CheckReport（run_check 的唯一实现）。

    降级契约集中在本函数，避免两个入口的策略分叉：
    - KB 失败（含适配器未预料的异常）→ kb_status=FAILED，报告无知识段落；
    - LLM 未配置或规模超限 → summary_only 基础报告；
    - LLM 已配置但输出不合契约 → 抛 LlmInvalidOutput（退出码 5），不返回半成品。
    """
    mode = profile.analysis_mode

    # 1) 知识库单次检索（失败降级为基础自检）
    kb_hits, kb_status = _search_kb(container, pr, profile, mode)

    # 2) LLM 综合（summary_only 或 LLM 未配置时，降级为基础风险报告）
    effective_mode = mode
    if mode != AnalysisMode.SUMMARY_ONLY and container.llm is None:
        effective_mode = AnalysisMode.SUMMARY_ONLY

    if effective_mode == AnalysisMode.SUMMARY_ONLY:
        report = _build_summary_only(
            pr, profile, kb_status, mode=effective_mode,
            degraded_no_llm=(mode != AnalysisMode.SUMMARY_ONLY),
        )
    else:
        diff_for_llm = diff_text if mode == AnalysisMode.FULL \
            else select_focused_diff(parsed, profile)
        system = build_system_prompt()
        user = build_user_prompt(pr, profile, diff_for_llm, kb_hits, mode)
        raw = container.llm.complete(system, user)
        sections = _parse_sections(raw)
        report = _assemble(pr, profile, sections, kb_hits, mode, kb_status)

    # 3) Evidence 后校验：valid_refs 取本次真实命中的 id，杜绝伪造引用
    return sanitize_report(report, valid_refs={h.id for h in kb_hits})


def _search_kb(container, pr: PRMetadata, profile, mode: AnalysisMode):
    """知识库单次检索。返回 (hits, status)。

    除 KbError 外，任何适配器未预料的异常同样降级为 FAILED —— 「KB 失败降级为
    基础自检」是显式契约，不能只依赖适配器边界的正确性。
    """
    if mode == AnalysisMode.SUMMARY_ONLY:
        return [], KbStatus.NOT_CONFIGURED
    if container.kb is None or not pr.project:
        # 无 KB 配置或缺少 project（KBQuery.project 必填，禁止跨项目检索）
        return [], KbStatus.NOT_CONFIGURED
    try:
        hits = container.kb.search(build_kb_query(pr, profile))
    except KbError:
        logger.error("kb_search_failed")
        return [], KbStatus.FAILED
    except Exception as exc:  # noqa: BLE001 - 降级契约优先于异常类型
        logger.error("kb_search_unexpected type=%s", type(exc).__name__)
        return [], KbStatus.FAILED
    return hits, (KbStatus.SUCCESS if hits else KbStatus.EMPTY)


def _parse_sections(raw: str) -> ReportSections:
    """解析并校验 LLM 输出的分段结构。

    JSON 可解析但结构不合法（字段缺失、枚举大小写错、类型不符）同样视为
    LLM 输出异常，统一抛 LlmInvalidOutput → 退出码 5，而非落到
    INTERNAL_ERROR（退出码 99）掩盖真实原因。
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LlmInvalidOutput(f"LLM 返回无法解析的 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise LlmInvalidOutput(f"LLM 输出应为 JSON 对象，实际为 {type(data).__name__}。")
    try:
        return ReportSections.model_validate(data)
    except PydanticValidationError as exc:
        raise LlmInvalidOutput(f"LLM 输出结构不符合约定：{_brief(exc)}") from exc


def _brief(exc: PydanticValidationError) -> str:
    """把 pydantic 校验错误压缩为「字段路径: 原因」列表，避免整页错误回显。"""
    parts = []
    for err in exc.errors()[:5]:
        loc = ".".join(str(x) for x in err.get("loc", ())) or "(root)"
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    return "; ".join(parts)


def _assemble(pr, profile, sections: ReportSections, kb_hits, mode, kb_status) -> CheckReport:
    hit_map = {h.id: h for h in kb_hits}

    # 无知识命中（未配置 / 空 / 检索失败）时，项目规范与技术债务段落必须为空，
    # 兑现「无知识不强判」的契约；否则 LLM 会凭空生成规范/债务条目污染报告。
    no_kb = kb_status in (KbStatus.NOT_CONFIGURED, KbStatus.EMPTY, KbStatus.FAILED)

    # kb_sources 由 source_refs 映射命中项；未命中源不列入（防止错误引用）
    seen_ids: set[str] = set()
    kb_sources: list[KbSource] = []
    for section in (sections.doc_check, sections.risk,
                    sections.project_rules, sections.tech_debt):
        for item in section:
            for ref in item.source_refs:
                if ref in seen_ids:
                    continue
                hit = hit_map.get(ref)
                if hit is not None:
                    seen_ids.add(ref)
                    kb_sources.append(KbSource(
                        id=hit.id, title=hit.title, doc_type=hit.doc_type,
                        project=hit.project, module=hit.module,
                    ))

    return CheckReport(
        meta=_meta(pr, profile, mode, kb_status),
        summary=sections.summary,
        doc_check=list(sections.doc_check), risk=list(sections.risk),
        project_rules=[] if no_kb else list(sections.project_rules),
        tech_debt=[] if no_kb else list(sections.tech_debt),
        manual_checklist=list(sections.manual_checklist) or _default_checklist(),
        kb_sources=kb_sources,
    )


def _build_summary_only(pr, profile, kb_status, *, mode: AnalysisMode | None = None,
                        degraded_no_llm: bool = False) -> CheckReport:
    """无可分析内容 / Large PR / LLM 未配置：仅变更摘要 + 基础风险 + 人工 Checklist。

    degraded_no_llm=True 表示本应深度分析但因 LLM 未配置而降级，摘要会注明原因。
    """
    risks: list[RiskItem] = []
    if profile.changed_files:
        # 没有可分析内容（空 diff）时不提示规模与高影响特征：不要谎报「规模较大」
        if not degraded_no_llm and profile.high_impact_features:
            risks.append(RiskItem(
                level=RiskLevel.LOW, text="本次 PR 规模较大，已仅对重点变更做辅助分析，不代表完成完整代码审查。",
                evidence_level=EvidenceLevel.C, source_refs=[],
            ))
        for f in profile.high_impact_features:
            risks.append(RiskItem(
                level=RiskLevel.MEDIUM, text=f"检测到高影响特征 {f.value}，建议人工重点确认。",
                evidence_level=EvidenceLevel.C, source_refs=[],
            ))
    return CheckReport(
        meta=_meta(pr, profile, mode or AnalysisMode.SUMMARY_ONLY, kb_status),
        summary=_summary_from_profile(pr, profile, degraded_no_llm=degraded_no_llm),
        doc_check=[], risk=risks, project_rules=[], tech_debt=[],
        manual_checklist=_default_checklist(),
        kb_sources=[],
    )


def _summary_from_profile(pr, profile, *, degraded_no_llm: bool = False) -> str:
    if profile.changed_files == 0:
        return (
            f"本次 PR「{pr.title}」未解析到可分析的代码变更"
            "（diff 为空、仅含二进制/权限变更，或 diff 格式无法识别），已跳过深度分析。"
        )
    parts = [f"本次 PR「{pr.title}」共变更 {profile.changed_files} 个文件、{profile.changed_lines} 行。"]
    if profile.change_types:
        parts.append("变更类型：" + "、".join(t.value for t in profile.change_types) + "。")
    if profile.modules:
        parts.append("涉及模块：" + "、".join(profile.modules) + "。")
    if degraded_no_llm:
        parts.append("LLM 未配置，已降级为仅基础风险自检（无 LLM 综合段落）；其余段落缺失不代表无问题，请结合人工审查。")
    else:
        parts.append("因规模超过深度分析范围，本报告仅提供变更摘要与基础自查提醒，不代表已完成代码审查。")
    return "".join(parts)


def _meta(pr, profile, mode, kb_status) -> ReportMeta:
    model = ""
    llm = get_container().llm
    if llm is not None:
        model = getattr(llm, "model", "") or ""
    return ReportMeta(
        pr_id=pr.pr_id, project=pr.project or pr.repository,
        model=model, analysis_mode=mode, kb_status=kb_status,
    )


def _default_checklist() -> list[str]:
    return [
        "API 兼容性", "测试覆盖", "异常和边界条件", "数据库迁移",
        "配置同步", "日志敏感信息", "文档同步",
    ]
