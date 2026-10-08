"""Pydantic v2 对外 Schema（C4 / M2 / CheckReport Schema / ADR §3）。

冻结字段，禁止自由文本枚举；统一 evidence_level 字段（兼容 C4 与 CheckReport 详版）。
"""
from __future__ import annotations

from datetime import datetime, UTC

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.enums import (
    AnalysisMode,
    ChangeType,
    DocCheckVerdict,
    EvidenceLevel,
    HighImpactFeature,
    KbStatus,
    RiskLevel,
    RuleVerdict,
    TechDebtVerdict,
)


# ===== 引用归一（C12 / D12） =====
class ProjectRef(BaseModel):
    id: int | None = None
    path: str | None = None

    @model_validator(mode="after")
    def _need_one(self) -> ProjectRef:
        if self.id is None and not self.path:
            raise ValueError("ProjectRef 至少需要 id 或 path 之一")
        return self


class MRRef(BaseModel):
    project: ProjectRef
    iid: int
    # 本地仓库直连（platform=local）时用于计算 diff / 合成元数据；远程平台忽略
    base_branch: str | None = None
    source_ref: str | None = None

    @property
    def pr_number(self) -> int:
        """平台中立的 MR/PR 编号（GitLab iid == GitHub pull_number）。

        适配器统一读取此属性，避免业务代码感知 iid / pull_number 差异。
        """
        return self.iid


# ===== PR 元数据 =====
class PRMetadata(BaseModel):
    project: str = ""
    repository: str = ""
    pr_id: int = 0
    title: str = ""
    description: str = ""
    source_branch: str = ""
    target_branch: str = ""
    author: str = ""
    updated_at: str = ""
    web_url: str = ""


# ===== Diff / 变更画像（C4） =====
class FileChange(BaseModel):
    path: str
    status: str  # added / modified / deleted / renamed
    additions: int = 0
    deletions: int = 0
    module: str = ""
    language: str = ""


class Symbol(BaseModel):
    name: str
    kind: str  # class / interface / enum / method / function / field ...
    file: str = ""
    change: str = "modified"


class ChangeProfile(BaseModel):
    changed_files: int = 0
    added_files: int = 0
    deleted_files: int = 0
    renamed_files: int = 0
    modules: list[str] = Field(default_factory=list)
    files: list[FileChange] = Field(default_factory=list)
    symbols: list[Symbol] = Field(default_factory=list)
    change_types: list[ChangeType] = Field(default_factory=list)
    api_changes: list[str] = Field(default_factory=list)
    data_changes: list[str] = Field(default_factory=list)
    config_changes: list[str] = Field(default_factory=list)
    dependency_changes: list[str] = Field(default_factory=list)
    logging_changes: list[str] = Field(default_factory=list)
    high_impact_features: list[HighImpactFeature] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    analysis_mode: AnalysisMode = AnalysisMode.FULL
    changed_lines: int = 0


# ===== KB 复合查询 / 命中（M2 / ADR §3.1） =====
class KBQuery(BaseModel):
    project: str
    modules: list[str] = Field(default_factory=list)
    pr_title: str = ""
    pr_description: str = ""
    key_files: list[str] = Field(default_factory=list)
    key_symbols: list[str] = Field(default_factory=list)
    change_types: list[str] = Field(default_factory=list)
    api_changes: list[str] = Field(default_factory=list)
    data_changes: list[str] = Field(default_factory=list)
    config_changes: list[str] = Field(default_factory=list)
    dependency_changes: list[str] = Field(default_factory=list)
    logging_changes: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    focus: list[str] = Field(default_factory=list)

    # 字段上限（M2）：避免 Context 膨胀
    @model_validator(mode="after")
    def _clip(self) -> KBQuery:
        self.modules = self.modules[:10]
        self.key_files = self.key_files[:20]
        self.key_symbols = self.key_symbols[:30]
        self.keywords = self.keywords[:20]
        self.api_changes = self.api_changes[:10]
        self.data_changes = self.data_changes[:10]
        self.config_changes = self.config_changes[:10]
        self.dependency_changes = self.dependency_changes[:10]
        self.logging_changes = self.logging_changes[:10]
        self.pr_title = self.pr_title[:200]
        self.pr_description = self.pr_description[:1000]
        return self


class KBHit(BaseModel):
    id: str
    title: str
    doc_type: str
    module: str = ""
    project: str = ""
    snippet: str = ""
    score: float = 0.0
    # 维度是否来自本地 KbDoc 元数据（而非供应商响应）。
    # Dify 片段不带 project / doc_type / module，adapter 用本地元数据回填后置 True；
    # 仍为 False 说明该命中没有本地元数据依据（他人上传或元数据库不可用），
    # 此时 project 只是「按查询回填」，隔离强度不足以支撑跨项目结论。
    metadata_resolved: bool = False


# ===== CheckReport（CheckReport Schema / ADR §3.2） =====
class ReportMeta(BaseModel):
    pr_id: int = 0
    project: str = ""
    # 稳定报告标识（R2）：project-branch-diffhash；供 feedback / metrics 引用。
    report_id: str = ""
    generated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    model: str = ""
    analysis_mode: AnalysisMode = AnalysisMode.FULL
    kb_status: KbStatus = KbStatus.NOT_CONFIGURED
    # 本次 LLM 综合是否命中缓存（R3）；summary_only / 未启用缓存时恒为 False。
    cache_hit: bool = False
    # 本次因证据规则而被降级 / 剥离 / 丢弃的条目数（P2）。
    # 之前这些修正全部静默发生：用户看到的是「干净的 C 级结论」，却不知道自己
    # 原本给出的 A 级强结论因为引用无效或类型不支撑而被降掉了。
    degraded_count: int = 0
    # 逐条说明（人可读）。仅在 PR_CHECK_DEBUG 开启时填充，避免默认输出过长。
    evidence_issues: list[str] = Field(default_factory=list)


class DocCheckItem(BaseModel):
    item: str
    verdict: DocCheckVerdict
    basis: str = ""
    advice: str = ""
    evidence_level: EvidenceLevel
    source_refs: list[str] = Field(default_factory=list)


class RiskItem(BaseModel):
    level: RiskLevel
    text: str
    # 文件:行号定位（可选）。LLM 参照 diff @@ hunk 头推断；填不出留空，不强校验。
    location: str = ""
    evidence_level: EvidenceLevel
    source_refs: list[str] = Field(default_factory=list)


class RuleItem(BaseModel):
    item: str
    verdict: RuleVerdict
    evidence_level: EvidenceLevel
    source_refs: list[str] = Field(default_factory=list)


class TechDebtItem(BaseModel):
    item: str
    verdict: TechDebtVerdict
    evidence_level: EvidenceLevel
    source_refs: list[str] = Field(default_factory=list)


class KbSource(BaseModel):
    id: str
    title: str
    doc_type: str
    project: str = ""
    module: str = ""


class CheckReport(BaseModel):
    meta: ReportMeta
    summary: str = ""
    doc_check: list[DocCheckItem] = Field(default_factory=list)
    risk: list[RiskItem] = Field(default_factory=list)
    project_rules: list[RuleItem] = Field(default_factory=list)
    tech_debt: list[TechDebtItem] = Field(default_factory=list)
    manual_checklist: list[str] = Field(default_factory=list)
    kb_sources: list[KbSource] = Field(default_factory=list)


class ReportSections(BaseModel):
    """LLM 原始分段输出契约（kb_sources 与 meta 由系统填充，LLM 无需输出）。

    与 CheckReport 的区别：这是 LLM 唯一被允许输出的形状。校验失败即
    LLM_INVALID_OUTPUT（退出码 5），不返回半成品报告。

    extra="ignore"：容忍 LLM 多吐出的未知字段，只强校验已知字段的存在性与取值。
    """

    model_config = ConfigDict(extra="ignore")

    summary: str = ""
    doc_check: list[DocCheckItem] = Field(default_factory=list)
    risk: list[RiskItem] = Field(default_factory=list)
    project_rules: list[RuleItem] = Field(default_factory=list)
    tech_debt: list[TechDebtItem] = Field(default_factory=list)
    manual_checklist: list[str] = Field(default_factory=list)
