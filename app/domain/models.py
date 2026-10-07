"""SQLModel 持久化模型（D14 / D15 / C2）。

仅保存：GitLab 连接 metadata（Token 加密）、KB 文档 metadata。
原始 Diff / 完整 PR 描述 / 报告不落库（ephemeral）。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class KbDoc(SQLModel, table=True):
    __tablename__ = "kb_docs"

    id: str = Field(default_factory=lambda: _new_id("kb"), primary_key=True)
    project: str = Field(index=True)
    module: str = Field(default="", index=True)
    doc_type: str = Field(index=True)
    title: str
    status: str = "active"
    snippet: str = ""
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class ReportFeedback(SQLModel, table=True):
    """报告条目反馈（R2）：只落「指纹」，不存报告原文。

    id 由 report_id + section + item_key 派生（确定性），因此同一目标的重复标记
    是覆盖而非追加——兑现 feedback 幂等契约。
    """

    __tablename__ = "report_feedback"

    id: str = Field(primary_key=True)
    report_id: str = Field(index=True)
    project: str = Field(default="", index=True)
    section: str = Field(index=True)
    item_key: str = Field(index=True)
    label: str = Field(index=True)  # fp（误报）/ useful（有用）
    note: str = ""
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class GateEvent(SQLModel, table=True):
    """闸门求值事件（R2）：每次带 --fail-on 的 check 记一条。

    id 由 report_id + specs 派生（确定性），因此同一变更重复 check 不会虚增次数
    ——统计口径是「受影响的不同报告数」，而非执行次数。
    """

    __tablename__ = "gate_events"

    id: str = Field(primary_key=True)
    report_id: str = Field(index=True)
    project: str = Field(default="", index=True)
    specs: str = ""  # 空格分隔的 --fail-on 规则（如 "risk:high rule:violation"）
    blocked: bool = False
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class AnalysisCache(SQLModel, table=True):
    """LLM 综合结果缓存（R3）：同一变更不重复烧 token。

    cache_key 含 diff / model / prompt 版本 / KB 命中集——任一变化即失效，
    保证新知识不会被旧缓存掩盖。hit_count 仅用于观测与 LRU 淘汰参考。
    """

    __tablename__ = "analysis_cache"

    cache_key: str = Field(primary_key=True)
    report_json: str
    model: str = ""
    prompt_version: str = ""
    project: str = Field(default="", index=True)
    hit_count: int = 0
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    last_hit_at: str = ""
