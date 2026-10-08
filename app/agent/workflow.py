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
from app.agent.evidence import sanitize_report, validate_report
from app.agent.kb_query import build_kb_query
from app.agent.prompt import build_system_prompt, build_user_prompt
from app.container import get_container, select_git_adapter
from app.domain.enums import (AnalysisMode, ChangeType, EvidenceLevel,
                              HighImpactFeature, KbStatus, RiskLevel)
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
    # 按 cred.platform 选择 Git 适配器（local 直连 .git / github / gitlab 只读 API）
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
    """KB 检索 → [缓存] → LLM 综合 → Evidence 校验 → CheckReport（唯一实现）。

    降级契约集中在本函数，避免两个入口的策略分叉：
    - KB 失败（含适配器未预料的异常）→ kb_status=FAILED，报告无知识段落；
    - LLM 未配置或规模超限 → summary_only 基础报告；
    - LLM 已配置但输出不合契约 → 抛 LlmInvalidOutput（退出码 5），不返回半成品。

    缓存（R3，默认关闭）：仅对 LLM 综合路径生效；命中即复用，且**仍走
    sanitize_report**——证据校验不可被缓存绕过。
    """
    from app.storage.cache import cache_enabled, get_cached, make_cache_key, put_cached

    mode = profile.analysis_mode
    report_id = compute_report_id(pr, diff_text)

    # 1) 知识库单次检索（失败降级为基础自检）
    kb_hits, kb_status = _search_kb(container, pr, profile, mode)
    valid_refs = {h.id for h in kb_hits}
    # 来源类型随命中项带入 evidence 规则 4：真实命中但类型与结论无关时也必须降级。
    ref_doc_types = {h.id: h.doc_type for h in kb_hits}

    # 2) LLM 综合（summary_only 或 LLM 未配置时，降级为基础风险报告）
    effective_mode = mode
    if mode != AnalysisMode.SUMMARY_ONLY and container.llm is None:
        effective_mode = AnalysisMode.SUMMARY_ONLY

    cache_key = ""
    if effective_mode != AnalysisMode.SUMMARY_ONLY and cache_enabled():
        cache_key = make_cache_key(
            diff_text, getattr(container.llm, "model", "") or "", valid_refs)
        cached = get_cached(cache_key)
        if cached is not None:
            cached.meta.cache_hit = True
            cleaned = sanitize_report(cached, valid_refs=valid_refs,
                                      ref_doc_types=ref_doc_types)
            return _annotate_degradation(raw=cached, cleaned=cleaned,
                                         valid_refs=valid_refs,
                                         ref_doc_types=ref_doc_types)

    if effective_mode == AnalysisMode.SUMMARY_ONLY:
        report = _build_summary_only(
            pr, profile, kb_status, mode=effective_mode,
            degraded_no_llm=(mode != AnalysisMode.SUMMARY_ONLY),
            report_id=report_id,
        )
    else:
        diff_for_llm = diff_text if mode == AnalysisMode.FULL \
            else select_focused_diff(parsed, profile)
        system = build_system_prompt()
        user = build_user_prompt(pr, profile, diff_for_llm, kb_hits, mode)
        raw = container.llm.complete(system, user)
        sections = _parse_sections(raw)
        report = _assemble(pr, profile, sections, kb_hits, mode, kb_status,
                           report_id=report_id)
        # 缓存「LLM 综合结果」（未 sanitize 的组装版）；读取时再校验证据。
        if cache_key:
            put_cached(cache_key, report,
                       getattr(container.llm, "model", "") or "", pr.project)

    # 3) Evidence 后校验：valid_refs 取本次真实命中的 id，杜绝伪造引用
    cleaned = sanitize_report(report, valid_refs=valid_refs,
                              ref_doc_types=ref_doc_types)
    return _annotate_degradation(raw=report, cleaned=cleaned,
                                 valid_refs=valid_refs,
                                 ref_doc_types=ref_doc_types)


def _annotate_degradation(*, raw: CheckReport, cleaned: CheckReport,
                          valid_refs: set[str],
                          ref_doc_types: dict[str, str]) -> CheckReport:
    """把证据规则的修正结果写进 meta，让降级对使用者可见。

    此前 validate_report 只被单测调用——生产链路里所有降级、剥离、丢弃都是静默的：
    用户看到的是一条干净的 C 级结论，却不知道自己给出的 A 级强结论因引用无效或
    类型不支撑而被改掉了。降级本身是设计意图，但**不可见的降级**会让人误以为
    模型原本就这么有把握。

    必须对**清洗前**的 raw 求值：清洗后的报告已经没有违规项，对它求值会恒为 0，
    等于什么都没统计。

    degraded_count 恒填充（一个数字，成本可忽略）；evidence_issues 逐条较长，
    仅在 PR_CHECK_DEBUG 开启时填充，同时打 stderr。
    summary / manual_checklist 的清洗是无条件策略而非「降级事件」，故不计入。
    """
    issues = validate_report(raw, valid_refs=valid_refs,
                             ref_doc_types=ref_doc_types)
    cleaned.meta.degraded_count = len(issues)
    if issues and _debug_enabled():
        cleaned.meta.evidence_issues = issues
        for line in issues:
            logger.warning("evidence_degraded: %s", line)
    return cleaned


def _debug_enabled() -> bool:
    import os

    return os.environ.get("PR_CHECK_DEBUG", "").strip().lower() in (
        "1", "true", "yes", "on")


def compute_report_id(pr: PRMetadata, diff_text: str) -> str:
    """稳定报告标识（R2）：project-branch-diffhash。

    同一变更（相同 diff + 分支 + 项目）重复自检得到相同 id，供 feedback / metrics
    引用；diff 一变即换新 id，避免反馈串到别的变更上。
    """
    import hashlib

    scope = (pr.project or pr.repository or "unknown").strip()
    branch = (pr.source_branch or "diff").strip() or "diff"
    digest = hashlib.sha256(
        f"{scope}|{branch}|{pr.pr_id}|{diff_text}".encode()
    ).hexdigest()[:12]
    slug = lambda s: s.replace("/", "-").replace("\\", "-").replace(" ", "-")  # noqa: E731
    return f"{slug(scope)}-{slug(branch)}-{digest}"


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
    # 按项目分库且该项目未绑定知识库：与「压根没配 KB」区分开，否则运维无法发现
    # 「配了多库却忘了给这个项目建」——那会让闸门静默永不触发。
    from app.adapters.routing_kb import RoutingKB

    if isinstance(container.kb, RoutingKB) and not container.kb.has_dataset(pr.project):
        logger.warning("kb_no_dataset project=%s configured=%s",
                       pr.project, container.kb.projects)
        return [], KbStatus.NO_DATASET
    try:
        hits = container.kb.search(build_kb_query(pr, profile))
    except KbError as exc:
        logger.error("kb_search_failed: %s", exc)
        return [], KbStatus.FAILED
    except Exception as exc:  # noqa: BLE001 - 降级契约优先于异常类型
        logger.error("kb_search_unexpected type=%s", type(exc).__name__)
        return [], KbStatus.FAILED
    hits = _drop_stale(hits, pr.project)
    return hits, (KbStatus.SUCCESS if hits else KbStatus.EMPTY)


def _drop_stale(hits, project: str):
    """剔除本地 metadata 中已标记 stale（过期）的来源（kb import --prune 的产物）。

    DB 查询异常时不过滤：检索结果优先于元数据，不能因本地库问题丢掉有效知识。
    """
    if not hits:
        return hits
    try:
        from app.storage.repo import list_stale_doc_ids

        stale = list_stale_doc_ids(project)
    except Exception as exc:  # noqa: BLE001 - 元数据不可用时放行检索结果
        logger.warning("kb_stale_filter_skipped type=%s", type(exc).__name__)
        return hits
    return [h for h in hits if h.id not in stale]


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


def _assemble(pr, profile, sections: ReportSections, kb_hits, mode, kb_status,
              *, report_id: str = "") -> CheckReport:
    hit_map = {h.id: h for h in kb_hits}

    # 无知识命中（未配置 / 空 / 检索失败）时，项目规范与技术债务段落必须为空，
    # 兑现「无知识不强判」的契约；否则 LLM 会凭空生成规范/债务条目污染报告。
    no_kb = kb_status in (KbStatus.NOT_CONFIGURED, KbStatus.EMPTY, KbStatus.FAILED,
                        KbStatus.NO_DATASET)

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
        meta=_meta(pr, profile, mode, kb_status, report_id=report_id),
        summary=sections.summary,
        doc_check=list(sections.doc_check), risk=list(sections.risk),
        project_rules=[] if no_kb else list(sections.project_rules),
        tech_debt=[] if no_kb else list(sections.tech_debt),
        manual_checklist=list(sections.manual_checklist) or build_checklist(profile),
        kb_sources=kb_sources,
    )


def _build_summary_only(pr, profile, kb_status, *, mode: AnalysisMode | None = None,
                        degraded_no_llm: bool = False,
                        report_id: str = "") -> CheckReport:
    """无可分析内容 / Large PR / LLM 未配置：仅变更摘要 + 基础风险 + 人工 Checklist。

    degraded_no_llm=True 表示本应深度分析但因 LLM 未配置而降级，摘要会注明原因。
    """
    risks: list[RiskItem] = []
    if profile.changed_files:
        # 没有可分析内容（空 diff）时不提示规模与高影响特征：不要谎报「规模较大」
        if not degraded_no_llm and profile.high_impact_features:
            risks.append(RiskItem(
                level=RiskLevel.LOW,
                text="本次 PR 规模较大，已仅对重点变更做辅助分析，"
                     "不代表完成完整代码审查。",
                evidence_level=EvidenceLevel.C, source_refs=[],
            ))
        for f in profile.high_impact_features:
            risks.append(RiskItem(
                level=RiskLevel.MEDIUM, text=f"检测到高影响特征 {f.value}，建议人工重点确认。",
                evidence_level=EvidenceLevel.C, source_refs=[],
            ))
    return CheckReport(
        meta=_meta(pr, profile, mode or AnalysisMode.SUMMARY_ONLY, kb_status,
                   report_id=report_id),
        summary=_summary_from_profile(pr, profile, degraded_no_llm=degraded_no_llm),
        doc_check=[], risk=risks, project_rules=[], tech_debt=[],
        manual_checklist=build_checklist(profile),
        kb_sources=[],
    )


def _summary_from_profile(pr, profile, *, degraded_no_llm: bool = False) -> str:
    if profile.changed_files == 0:
        return (
            f"本次 PR「{pr.title}」未解析到可分析的代码变更"
            "（diff 为空、仅含二进制/权限变更，或 diff 格式无法识别），已跳过深度分析。"
        )
    parts = [f"本次 PR「{pr.title}」共变更 {profile.changed_files} 个文件、"
             f"{profile.changed_lines} 行。"]
    if profile.change_types:
        parts.append("变更类型：" + "、".join(t.value for t in profile.change_types) + "。")
    if profile.modules:
        parts.append("涉及模块：" + "、".join(profile.modules) + "。")
    if degraded_no_llm:
        parts.append("LLM 未配置，已降级为仅基础风险自检（无 LLM 综合段落）；"
                     "其余段落缺失不代表无问题，请结合人工审查。")
    else:
        parts.append("因规模超过深度分析范围，本报告仅提供变更摘要与基础自查提醒，不代表已完成代码审查。")
    return "".join(parts)


def _meta(pr, profile, mode, kb_status, *, report_id: str = "") -> ReportMeta:
    model = ""
    llm = get_container().llm
    if llm is not None:
        model = getattr(llm, "model", "") or ""
    return ReportMeta(
        pr_id=pr.pr_id, project=pr.project or pr.repository,
        report_id=report_id,
        model=model, analysis_mode=mode, kb_status=kb_status,
    )


# checklist 基础项：任何 PR 都应人工过目（与变更画像无关，保证清单非空）
_BASE_CHECKLIST = [
    "测试覆盖", "异常和边界条件", "文档同步",
]

# 变更画像触发器 → 专项人工检查项（P2-10）。
# 同一主题的 ChangeType 与 HighImpactFeature 指向相同文本（如 DATABASE_CHANGE 与
# DATABASE），追加时按文本去重：任一侧命中即出一项，不会重复。未登记的触发器
# （COMMENT_CHANGE / TEST_CHANGE 等无风险主题）不产生专项项，靠基础项保底。
_CHECKLIST_BY_TRIGGER: dict = {
    # 接口契约
    ChangeType.API_CHANGE: "API 兼容性（调用方是否需要同步改造、契约是否版本化）",
    HighImpactFeature.PUBLIC_API: "API 兼容性（调用方是否需要同步改造、契约是否版本化）",
    # 数据与存储
    ChangeType.DATA_MODEL_CHANGE: "数据模型变更兼容性（历史数据与回滚）",
    ChangeType.DATABASE_CHANGE: "数据库迁移与回滚脚本（索引、锁、数据量评估）",
    HighImpactFeature.DATABASE: "数据库迁移与回滚脚本（索引、锁、数据量评估）",
    # 配置与依赖
    ChangeType.CONFIG_CHANGE: "配置同步（各环境默认值是否一致）",
    HighImpactFeature.CONFIGURATION: "配置同步（各环境默认值是否一致）",
    ChangeType.DEPENDENCY_CHANGE: "依赖变更影响面（版本锁定、降级与超时策略）",
    HighImpactFeature.EXTERNAL_DEPENDENCY: "依赖变更影响面（版本锁定、降级与超时策略）",
    # 可观测性
    ChangeType.LOGGING_CHANGE: "日志可观测性（敏感字段脱敏、关键路径有迹）",
    HighImpactFeature.LOGGING: "日志可观测性（敏感字段脱敏、关键路径有迹）",
    # 运行时属性
    ChangeType.TRANSACTION_CHANGE: "事务边界与异常回滚",
    HighImpactFeature.TRANSACTION: "事务边界与异常回滚",
    ChangeType.CACHE_CHANGE: "缓存一致性与失效策略",
    HighImpactFeature.CACHE: "缓存一致性与失效策略",
    ChangeType.AUTH_CHANGE: "权限与鉴权变更验证",
    HighImpactFeature.PERMISSION: "权限与鉴权变更验证",
    ChangeType.SERIALIZATION_CHANGE: "序列化向后兼容（新老格式混部）",
    HighImpactFeature.SERIALIZATION: "序列化向后兼容（新老格式混部）",
    HighImpactFeature.CONCURRENCY: "并发安全（竞态、死锁）",
}

# 文件操作 → 追加确认项（added/deleted/renamed 计数 > 0 时逐项追加）
_CHECKLIST_BY_FILE_OP = [
    ("added", "新增文件是否纳入构建/发布/忽略规则"),
    ("deleted", "删除文件的影响面（残留引用是否清理干净）"),
    ("renamed", "重命名可追溯性（git 是否识别为 rename、引用是否同步）"),
]


def build_checklist(profile) -> list[str]:
    """按变更画像生成人工清单：基础项保底 + 画像触发的专项项 + 文件操作确认项。

    - LLM 输出空 checklist 时（或 summary_only 无 LLM 时）的回落值；
    - 专项项按映射表声明顺序输出（不随检测顺序漂移），同主题文本去重；
    - 空画像退化为纯基础项，清单永不落空（渲染层依赖非空）。
    """
    triggers = set(profile.change_types) | set(profile.high_impact_features)
    items = list(_BASE_CHECKLIST)
    seen = set(items)
    for trigger, text in _CHECKLIST_BY_TRIGGER.items():
        if trigger in triggers and text not in seen:
            seen.add(text)
            items.append(text)
    for attr, text in _CHECKLIST_BY_FILE_OP:
        if getattr(profile, f"{attr}_files", 0):
            items.append(text)
    return items
