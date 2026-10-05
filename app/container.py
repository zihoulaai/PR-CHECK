"""适配器容器：组装 GitLab / LLM / KB 实现，并支持测试注入 Fake。

默认从配置构建真实实现；测试或离线运行时通过 set_adapters / 环境变量覆盖。
延迟导入避免循环与缺模块时启动失败。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Container:
    git: object | None = None
    llm: object | None = None
    kb: object | None = None


_holder: Container | None = None


def _build_default() -> Container:
    from app.config import get_settings
    from app.adapters.fakes import FakeGitLab, FakeLLM, FakeKB

    s = get_settings()
    # GitLab：MVP 仅 GitLabAdapter 一种真实实现（adapters 步骤落地）。
    # 若环境变量显式要求离线 fake（TESTING），使用 Fake。
    import os

    use_fake = os.getenv("PR_CHECK_USE_FAKE") == "1"

    git = None
    llm = None
    kb = None

    if use_fake:
        git = FakeGitLab()
        llm = FakeLLM()
        kb = FakeKB()
    else:
        # 真实实现在 adapters 步骤创建；此处延迟引用
        from app.adapters.gitlab import GitLabAdapter
        from app.adapters.llm import LLMClientImpl
        from app.adapters.maas_kb import MaaSVectorKBAdapter

        git = GitLabAdapter()
        llm = LLMClientImpl(base_url=s.llm_base_url, model=s.llm_model, api_key=s.llm_api_key,
                            timeout=s.llm_timeout_seconds, max_retries=s.llm_max_retries) if s.llm_base_url else None
        kb = MaaSVectorKBAdapter(base_url=s.kb_base_url, api_key=s.kb_api_key, index=s.kb_index) if s.kb_base_url else None

    return Container(git=git, llm=llm, kb=kb)


def get_container() -> Container:
    global _holder
    if _holder is None:
        _holder = _build_default()
    return _holder


def set_container(c: Container) -> None:
    global _holder
    _holder = c


def reset_container() -> None:
    global _holder
    _holder = None
