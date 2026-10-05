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


class GitConnection(SQLModel, table=True):
    __tablename__ = "git_connections"

    id: str = Field(default="gitlab-default", primary_key=True)
    name: str
    base_url: str
    # 加密后的 Token 字符串（Fernet token 文本），绝不存明文
    encrypted_token: str = ""


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
