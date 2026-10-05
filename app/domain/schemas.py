"""Pydantic v2 对外 Schema（C4 / M2 / CheckReport Schema / ADR §3）。

冻结字段，禁止自由文本枚举；统一 evidence_level 字段（兼容 C4 与 CheckReport 详版）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field, model_validator

from app.domain.enums import (
    AnalysisMode,
    ChangeType,
    DocCheckVerdict,
    DocType,
    EvidenceLevel,
    HighImpactFeature,
    KbStatus,
    RiskLevel,
    RuleVerdict,
    TechDebtVerdict,
)


# ===== 引用归一（C12 / D12） =====
class ProjectRef(BaseModel):
    id: Optional[int] = None
    path: Optional[str] = None

    @model_validator(mode="after")
    def _need_one(self) -> "ProjectRef":
        if self.id is None and not self.path:
            raise ValueError("ProjectRef 至少需要 id 或 path 之一")
        return self


class MRRef(BaseModel):
    project: ProjectRef
    iid: int


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
    comment_changes: list[str] = Field(default_factory=list)
    test_changes: list[str] = Field(default_factory=list)
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
    logging_changes: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    focus: list[str] = Field(default_factory=list)

    # 字段上限（M2）：避免 Context 膨胀
    @model_validator(mode="after")
    def _clip(self) -> "KBQuery":
        self.modules = self.modules[:10]
        self.key_files = self.key_files[:20]
        self.key_symbols = self.key_symbols[:30]
        self.keywords = self.keywords[:20]
        self.api_changes = self.api_changes[:10]
        self.data_changes = self.data_changes[:10]
        self.config_changes = self.config_changes[:10]
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


# ===== CheckReport（CheckReport Schema / ADR §3.2） =====
class ReportMeta(BaseModel):
    pr_id: int = 0
    project: str = ""
    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    model: str = ""
    analysis_mode: AnalysisMode = AnalysisMode.FULL
    kb_status: KbStatus = KbStatus.NOT_CONFIGURED


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


# ===== 请求 / 响应 =====
class CheckRequest(BaseModel):
    # 方式 A：浏览选择
    connection_id: Optional[str] = None
    project_id: Optional[int] = None
    project_path: Optional[str] = None
    mr_iid: int

    @model_validator(mode="after")
    def _need_project(self) -> "CheckRequest":
        if self.project_id is None and not self.project_path:
            raise ValueError("需要 project_id 或 project_path 其中之一")
        return self


class CheckResponse(BaseModel):
    report: CheckReport
    rendered_markdown: str = ""


class SettingsGitLabIn(BaseModel):
    name: str
    base_url: str
    token: str


class SettingsGitLabOut(BaseModel):
    id: str
    name: str
    base_url: str


class SettingsOut(BaseModel):
    gitlab: dict
    llm: dict
    kb: dict
