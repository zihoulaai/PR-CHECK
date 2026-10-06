"""KB 适配器注册表 / 工厂路由测试。

覆盖：未配置回落 None、maas / openai 路由、未知 provider 显式报错、
top_k 配置真正注入 adapter（修复此前硬编码 5 的不一致）。
"""
from __future__ import annotations

import pytest

from app.adapters.maas_kb import MaaSVectorKBAdapter
from app.adapters.openai_kb import OpenAIStyleKBAdapter
from app.adapters.registry import build_kb
from app.config import Settings
from app.errors import KbError


def _settings(**overrides) -> Settings:
    base = dict(kb_base_url="http://kb.local", kb_api_key="k")
    base.update(overrides)
    return Settings(**base)


def test_no_credentials_returns_none():
    assert build_kb(Settings(kb_base_url=None, kb_api_key=None)) is None
    assert build_kb(Settings(kb_base_url="x", kb_api_key=None)) is None


def test_default_provider_is_maas():
    kb = build_kb(_settings())
    assert isinstance(kb, MaaSVectorKBAdapter)


def test_provider_openai_routes_to_openai_adapter():
    kb = build_kb(_settings(kb_provider="openai"))
    assert isinstance(kb, OpenAIStyleKBAdapter)


def test_unknown_provider_raises_kb_error():
    with pytest.raises(KbError):
        build_kb(_settings(kb_provider="dify"))


def test_top_k_injected_into_adapter():
    # 修复前 top_k 在 adapter 内写死 5，配置 kb_top_k 无效
    kb = build_kb(_settings(kb_provider="maas", kb_top_k=3))
    assert isinstance(kb, MaaSVectorKBAdapter)
    assert kb.top_k == 3

    kb2 = build_kb(_settings(kb_provider="openai", kb_top_k=7))
    assert isinstance(kb2, OpenAIStyleKBAdapter)
    assert kb2.top_k == 7


def test_provider_case_insensitive():
    kb = build_kb(_settings(kb_provider="OpenAI"))
    assert isinstance(kb, OpenAIStyleKBAdapter)
