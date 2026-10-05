"""适配器抽象接口（D3 / D4 / D5 / ADR §2.2）。

业务代码只依赖这三个 Protocol，不感知具体供应商实现：
- GitPlatformAdapter：Git 是数据源，不是 Agent 工具
- LLMClient：模型 / Endpoint / 鉴权全配置化
- KnowledgeBase：Vector KB 对 Agent 透明；project 必填，服务端按 project 过滤
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.domain.schemas import KBHit, KBQuery, MRRef, PRMetadata, ProjectRef


@dataclass
class GitCredential:
    """解密后的 GitLab 连接凭据（仅在调用 Adapter 的瞬间存在，不落库/不进 LLM）。"""

    base_url: str
    token: str


@dataclass
class ProjectItem:
    id: int
    path: str
    path_with_namespace: str = ""
    web_url: str = ""


@dataclass
class MRItem:
    iid: int
    title: str
    source_branch: str = ""
    target_branch: str = ""
    updated_at: str = ""
    author: str = ""


@dataclass
class KbDocInput:
    project: str
    module: str
    doc_type: str
    title: str
    content: str


@runtime_checkable
class GitPlatformAdapter(Protocol):
    def list_projects(self, cred: GitCredential, *, search: str | None = None,
                      page: int = 1, per_page: int = 20) -> list[ProjectItem]: ...
    def list_mrs(self, cred: GitCredential, proj: ProjectRef, *, state: str = "opened",
                 page: int = 1, per_page: int = 20) -> list[MRItem]: ...
    def get_mr(self, cred: GitCredential, ref: MRRef) -> PRMetadata: ...
    def get_diff(self, cred: GitCredential, ref: MRRef) -> str: ...


@runtime_checkable
class LLMClient(Protocol):
    def complete(self, system: str, user: str) -> str: ...


@runtime_checkable
class KnowledgeBase(Protocol):
    def search(self, query: KBQuery) -> list[KBHit]: ...
    def upload(self, doc: KbDocInput) -> str: ...
