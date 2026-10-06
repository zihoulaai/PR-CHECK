"""KB 适配器注册表 + 工厂（供应商可插拔框架）。

业务层（container / workflow）只通过 ``build_kb`` 拿到一个 ``KnowledgeBase``
实例，不感知具体供应商。新增供应商只需：
1. 在 ``app/adapters/`` 下实现 ``KnowledgeBase``（search / upload）；
2. 在下方 ``PROVIDERS`` 注册表登记。

路由键由配置 ``kb_provider`` 驱动，未知值显式抛 ``KbError``（不静默回落到
MaaS，避免误配导致用错库）。未配置 ``kb_base_url`` / ``kb_api_key`` 时返回
``None``，由 workflow 降级为基础自检（与现状一致）。
"""
from __future__ import annotations

from app.adapters.base import KnowledgeBase
from app.adapters.maas_kb import MaaSVectorKBAdapter
from app.adapters.openai_kb import OpenAIStyleKBAdapter
from app.adapters.dify_kb import DifyKBAdapter
from app.config import Settings
from app.errors import KbError

PROVIDERS: dict[str, type[KnowledgeBase]] = {
    "maas": MaaSVectorKBAdapter,
    "openai": OpenAIStyleKBAdapter,
    "dify": DifyKBAdapter,
}

DEFAULT_PROVIDER = "maas"


def build_kb(s: Settings) -> KnowledgeBase | None:
    """按配置选择 KB 适配器。

    - 未配置凭据 → 返回 ``None``（降级基础自检）。
    - ``kb_provider`` 未登记 → 抛 ``KbError``（显式报错，不静默回落）。
    """
    if not s.kb_base_url or not s.kb_api_key:
        return None

    provider = (s.kb_provider or DEFAULT_PROVIDER).strip().lower()
    cls = PROVIDERS.get(provider)
    if cls is None:
        raise KbError(
            f"未知的 KB_PROVIDER={provider!r}；可选：{', '.join(sorted(PROVIDERS))}"
        )

    return cls(
        base_url=s.kb_base_url,
        api_key=s.kb_api_key,
        index=s.kb_index,
        top_k=s.kb_top_k,
    )
