"""Agent 线性工作流（ADR §2 / PRD §4 / D11）。

流程：归一引用 → Git 获取（失败即终止）→ Diff 解析 → ChangeProfile →
（KB 单次检索，失败降级）→ LLM 综合（失败整体失败）→ Evidence 校验 → CheckReport。
无循环、无 Reflection、无多轮检索；Git 是数据源非 Agent 工具（Agent 唯一工具为 KB 检索）。
"""
from __future__ import annotations

import json
import logging

from app.adapters.base import GitCredential
from app.agent.evidence import sanitize_report
from app.agent.kb_query import build_kb_query
from app.agent.prompt import build_system_prompt, build_user_prompt
from app.container import get_container, select_git_adapter
from app.domain.enums import AnalysisMode, DocCheckVerdict, EvidenceLevel, KbStatus, RiskLevel
from app.domain.schemas import (
    CheckReport, DocCheckItem, KBHit, KbSource, MRRef, PRMetadata, RiskItem,
    RuleItem, TechDebtItem,
)
from app.errors import KbError, LlmInvalidOutput, NotConfiguredError
from app.parser.change_profile import build_change_profile, select_focused_diff
from app.parser.diff_parser import parse_diff

logger = logging.getLogger("pr_check")


def run_check(cred: GitCredential, mr_ref: MRRef) -> CheckReport:
    container = get_container()
    # 选择本地 Git 适配器（直连 .git，无需 Token）
    git = select_git_adapter(cred.platform)

    # 1) Git 获取（失败即终止，不进入 Agent）
    pr: PRMetadata = git.get_mr(cred, mr_ref)
    diff_text: str = git.get_diff(cred, mr_ref)

    # 2) Diff 解析 + 变更画像
    parsed = parse_diff(diff_text)
    profile = build_change_profile(pr, parsed)
    mode = profile.analysis_mode

    # 3) 知识库单次检索（失败降级为基础自检）
    kb_hits: list[KBHit] = []
    if mode == AnalysisMode.SUMMARY_ONLY:
        kb_status = KbStatus.NOT_CONFIGURED
    elif container.kb is not None:
        try:
            query = build_kb_query(pr, profile)
            kb_hits = container.kb.search(query)
            kb_status = KbStatus.SUCCESS if kb_hits else KbStatus.EMPTY
        except KbError as exc:
            logger.error("kb_search_failed")
            kb_status = KbStatus.FAILED
            kb_hits = []
    else:
        kb_status = KbStatus.NOT_CONFIGURED

    # 4) LLM 综合（summary_only 不进完整 LLM 分析）
    if mode == AnalysisMode.SUMMARY_ONLY:
        report = _build_summary_only(pr, profile, kb_status)
    else:
        if container.llm is None:
            raise NotConfiguredError("LLM 未配置，无法生成完整自检报告。")
        diff_for_llm = diff_text if mode == AnalysisMode.FULL \
            else select_focused_diff(parsed, profile)
        system = build_system_prompt()
        user = build_user_prompt(pr, profile, diff_for_llm, kb_hits, mode)
        raw = container.llm.complete(system, user)
        try:
            sections = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LlmInvalidOutput(f"LLM 返回无法解析的 JSON：{exc}") from exc
        report = _assemble(pr, profile, sections, kb_hits, mode, kb_status)
        report = sanitize_report(report)

    return report


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

    链路与 run_check 一致（parse_diff → build_change_profile → KB 检索 →
    LLM 综合 → sanitize_report）。当 LLM 未配置时降级为 summary_only 基础报告，
    保证离线（如 CLI --fake 无真实 LLM）也能产出可用结果。
    """
    container = get_container()

    pr = PRMetadata(
        project=project, repository=project, pr_id=0, title=title,
        description=description, source_branch=source_branch,
        target_branch=target_branch, author=author,
    )

    parsed = parse_diff(diff_text)
    profile = build_change_profile(pr, parsed)
    mode = profile.analysis_mode

    # KB 检索（与 run_check 一致的失败降级逻辑）
    kb_hits: list[KBHit] = []
    if mode == AnalysisMode.SUMMARY_ONLY:
        kb_status = KbStatus.NOT_CONFIGURED
    elif container.kb is not None and pr.project:
        try:
            query = build_kb_query(pr, profile)
            kb_hits = container.kb.search(query)
            kb_status = KbStatus.SUCCESS if kb_hits else KbStatus.EMPTY
        except KbError as exc:
            logger.error("kb_search_failed")
            kb_status = KbStatus.FAILED
            kb_hits = []
    else:
        # 无 KB 配置或缺少 project（KBQuery.project 必填，禁止跨项目检索）
        kb_status = KbStatus.NOT_CONFIGURED

    # 无 LLM 或未达深度分析规模：降级为基础自检
    effective_mode = mode
    if mode != AnalysisMode.SUMMARY_ONLY and container.llm is None:
        effective_mode = AnalysisMode.SUMMARY_ONLY

    if effective_mode == AnalysisMode.SUMMARY_ONLY:
        report = _build_summary_only(pr, profile, kb_status, mode=effective_mode)
    else:
        diff_for_llm = diff_text if mode == AnalysisMode.FULL \
            else select_focused_diff(parsed, profile)
        system = build_system_prompt()
        user = build_user_prompt(pr, profile, diff_for_llm, kb_hits, mode)
        raw = container.llm.complete(system, user)
        try:
            sections = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LlmInvalidOutput(f"LLM 返回无法解析的 JSON：{exc}") from exc
        report = _assemble(pr, profile, sections, kb_hits, mode, kb_status)
        report = sanitize_report(report)

    return report


def _assemble(pr, profile, sections, kb_hits, mode, kb_status) -> CheckReport:
    hit_map = {h.id: h for h in kb_hits}

    doc_check = [DocCheckItem(**d) for d in sections.get("doc_check", [])]
    risk = [RiskItem(**d) for d in sections.get("risk", [])]
    project_rules = [RuleItem(**d) for d in sections.get("project_rules", [])]
    tech_debt = [TechDebtItem(**d) for d in sections.get("tech_debt", [])]
    manual_checklist = sections.get("manual_checklist", []) or _default_checklist()

    # kb_sources 由 source_refs 映射命中项；未命中源不列入（防止错误引用）
    seen_ids: set[str] = set()
    kb_sources: list[KbSource] = []
    for section in (doc_check, risk, project_rules, tech_debt):
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
        summary=sections.get("summary", ""),
        doc_check=doc_check, risk=risk, project_rules=project_rules,
        tech_debt=tech_debt, manual_checklist=manual_checklist,
        kb_sources=kb_sources,
    )


def _build_summary_only(pr, profile, kb_status, *, mode: AnalysisMode | None = None) -> CheckReport:
    """Large PR：仅变更摘要 + 基础风险 + 人工 Checklist，不做项目规范/历史债务强匹配。"""
    risks: list[RiskItem] = []
    if any(t.value for t in profile.high_impact_features):
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
        summary=_summary_from_profile(pr, profile),
        doc_check=[], risk=risks, project_rules=[], tech_debt=[],
        manual_checklist=_default_checklist(),
        kb_sources=[],
    )


def _summary_from_profile(pr, profile) -> str:
    parts = [f"本次 PR「{pr.title}」共变更 {profile.changed_files} 个文件、{profile.changed_lines} 行。"]
    if profile.change_types:
        parts.append("变更类型：" + "、".join(t.value for t in profile.change_types) + "。")
    if profile.modules:
        parts.append("涉及模块：" + "、".join(profile.modules) + "。")
    parts.append("因规模超过深度分析范围，本报告仅提供变更摘要与基础自查提醒，不代表已完成代码审查。")
    return "".join(parts)


def _meta(pr, profile, mode, kb_status) -> "ReportMeta":
    from app.domain.schemas import ReportMeta
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
