"""适配器注册表 + 工厂（Git 平台 / KB 供应商可插拔框架）。

业务层（container / workflow）只通过 ``build_kb`` / ``build_git`` 拿到适配器实例，
不感知具体实现。新增供应商或平台只需：
1. 在 ``app/adapters/`` 下实现对应 Protocol（KnowledgeBase / GitPlatformAdapter）；
2. 在下方 ``PROVIDERS`` / ``GIT_ADAPTERS`` 注册表登记。

KB 路由键由配置 ``kb_provider`` 驱动，未知值显式抛 ``KbError``（不静默回落到
MaaS，避免误配导致用错库）。未配置 ``kb_base_url`` / ``kb_api_key`` 时返回
``None``，由 workflow 降级为基础自检（与现状一致）。

Git 平台路由键由 ``git_platform``（或 ``select_git_adapter`` 入参）驱动，
``local`` 走本地 .git，``github`` / ``gitlab`` 走远端只读 API（R4）。
"""
from __future__ import annotations

from app.adapters.base import (
    GitPlatformAdapter,
    GitPlatformAdapterFactory,
    KnowledgeBase,
    KnowledgeBaseFactory,
)
from app.adapters.dify_kb import DifyKBAdapter
from app.adapters.github import GitHubAdapter
from app.adapters.gitlab import GitLabAdapter
from app.adapters.local_git import LocalGitAdapter
from app.adapters.maas_kb import MaaSVectorKBAdapter
from app.adapters.openai_kb import OpenAIStyleKBAdapter
from app.config import Settings, dataset_mapping
from app.domain.enums import Platform
from app.errors import GitUnavailable, KbError

# 注册表存的是**类**，故按构造契约（Factory）标注而非实例协议：
# 否则 mypy 会拿实例协议的空 __init__ 去校验 cls(base_url=...)，报出一堆
# 与真实错误无关的「Unexpected keyword argument」。
PROVIDERS: dict[str, type[KnowledgeBaseFactory]] = {
    "maas": MaaSVectorKBAdapter,
    "openai": OpenAIStyleKBAdapter,
    "dify": DifyKBAdapter,
}

DEFAULT_PROVIDER = "maas"

# Git 平台注册表：platform 取值 -> 适配器类（本地适配器无参数，单独处理）
GIT_ADAPTERS: dict[str, type[GitPlatformAdapterFactory]] = {
    Platform.GITHUB.value: GitHubAdapter,
    Platform.GITLAB.value: GitLabAdapter,
}


def build_kb(s: Settings) -> KnowledgeBase | None:
    """按配置选择 KB 适配器。

    - 未配置凭据 → 返回 ``None``（降级基础自检）。
    - ``kb_provider`` 未登记 → 抛 ``KbError``（显式报错，不静默回落）。

    配置了 ``KB_DATASET_MAP``（按项目分库）时，为映射里的每个项目各建一个适配器，
    交给 ``RoutingKB`` 按 project 路由；此时**未命中映射的项目视为没有知识库**，
    不会回落到单一 ``kb_index``。未配置映射时行为与之前完全一致。
    """
    if not s.kb_base_url or not s.kb_api_key:
        return None

    provider = (s.kb_provider or DEFAULT_PROVIDER).strip().lower()
    cls = PROVIDERS.get(provider)
    if cls is None:
        raise KbError(
            f"未知的 KB_PROVIDER={provider!r}；可选：{', '.join(sorted(PROVIDERS))}"
        )

    mapping = dataset_mapping(s)
    if mapping:
        from app.adapters.routing_kb import RoutingKB

        return RoutingKB(
            {project: _make_kb(cls, s, index) for project, index in mapping.items()},
            strict=True,
        )

    return _make_kb(cls, s, s.kb_index)


def _make_kb(cls: type[KnowledgeBaseFactory], s: Settings,
             index: str | None) -> KnowledgeBase:
    return cls(
        base_url=s.kb_base_url,
        api_key=s.kb_api_key,
        index=index,
        top_k=s.kb_top_k,
    )


def build_git(s: Settings, platform: str = "") -> GitPlatformAdapter:
    """按平台选择 Git 适配器（R4）。

    - ``local``（默认）→ LocalGitAdapter（直连 .git，无需 Token）。
    - ``github`` / ``gitlab`` → 远端只读适配器，凭据取 ``git_base_url`` / ``git_token``。
    - 未登记的平台 → 抛 ``GitUnavailable``（显式报错，退出码 4，不静默回落本地）。
    """
    key = (platform or s.git_platform or Platform.LOCAL.value).strip().lower()
    if key == Platform.LOCAL.value:
        return LocalGitAdapter()

    cls = GIT_ADAPTERS.get(key)
    if cls is None:
        raise GitUnavailable(
            f"未知的 Git 平台 {key!r}；可选："
            f"{', '.join([Platform.LOCAL.value, *sorted(GIT_ADAPTERS)])}"
        )
    return cls(
        base_url=s.git_base_url or "",
        token=s.git_token or "",
        timeout=s.llm_timeout_seconds,
    )
