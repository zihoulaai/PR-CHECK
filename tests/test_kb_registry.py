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
    # 显式固定 kb_provider：pydantic-settings 会从 .env / 环境变量读取该字段
    #（例如本地 .env 的 KB_PROVIDER=dify），测试必须自包含、不依赖真实环境。
    # 显式 init 参数优先级高于环境变量与 dotenv，其余用例可用 overrides 覆盖。
    base = dict(kb_base_url="http://kb.local", kb_api_key="k", kb_provider="maas")
    base.update(overrides)
    return Settings(**base)


def test_no_credentials_returns_none():
    # 显式 app_env=dev：本机用户级 .env 若为 prod 会触发强校验，与本用例意图无关，
    # 测试结果不应依赖环境文件（显式构造参数优先级最高，凭据传 None 即缺省）。
    assert build_kb(Settings(app_env="dev", kb_base_url=None, kb_api_key=None)) is None
    assert build_kb(Settings(app_env="dev", kb_base_url="x", kb_api_key=None)) is None


def test_default_provider_is_maas():
    kb = build_kb(_settings())
    assert isinstance(kb, MaaSVectorKBAdapter)


def test_provider_openai_routes_to_openai_adapter():
    kb = build_kb(_settings(kb_provider="openai"))
    assert isinstance(kb, OpenAIStyleKBAdapter)


def test_unknown_provider_raises_kb_error():
    # dify 已在注册表登记（DifyKBAdapter），不再是「未知」供应商；
    # 用未登记的名称验证「未知 provider 显式报错、不静默回落」的契约。
    with pytest.raises(KbError):
        build_kb(_settings(kb_provider="nonexistent"))


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
