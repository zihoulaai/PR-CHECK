"""适配器容器：组装本地 Git / LLM / KB 实现，并支持测试注入 Fake。

- 本地自检优先：git_adapters 仅持有 LocalGitAdapter（直连 .git，无需 Token）；
- 默认从配置构建真实实现；测试或离线运行通过 set_adapters / 环境变量覆盖。
"""
from __future__ import annotations

import os
from typing import Optional

from app.adapters.base import GitPlatformAdapter
from app.domain.enums import Platform


class Container:
    def __init__(self, git: Optional[GitPlatformAdapter] = None,
                 llm: object | None = None, kb: object | None = None):
        self._git = git
        self.llm = llm
        self.kb = kb
        self.git_adapters: dict[str, GitPlatformAdapter] = {}
        if git is not None:
            self.git_adapters[Platform.LOCAL.value] = git

    @property
    def git(self) -> Optional[GitPlatformAdapter]:
        return self._git

    @git.setter
    def git(self, value: Optional[GitPlatformAdapter]) -> None:
        self._git = value
        if value is not None:
            # 兼容旧单测：直接注入 container.git 即视为本地适配器
            self.git_adapters[Platform.LOCAL.value] = value


_holder: Container | None = None


def _build_default() -> Container:
    from app.adapters.fakes import FakeKB, FakeLLM
    from app.adapters.local_git import LocalGitAdapter
    from app.config import get_settings

    s = get_settings()
    use_fake = os.getenv("PR_CHECK_USE_FAKE") == "1"

    if use_fake:
        # 仅 LLM / KB 走 Fake；本地 Git 适配器保持真实（直连 .git），--fake 不影响 --repo 自检
        llm = FakeLLM()
        kb = FakeKB()
    else:
        from app.adapters.llm import LLMClientImpl
        from app.adapters.registry import build_kb
        from app.domain.schemas import ReportSections

        llm = LLMClientImpl(
            base_url=s.llm_base_url, model=s.llm_model, api_key=s.llm_api_key,
            timeout=s.llm_timeout_seconds, max_retries=s.llm_max_retries,
            # 结构化输出：服务端按 Schema 约束形状；不支持时客户端自动降级 json_object
            response_schema=ReportSections.model_json_schema(),
            enable_thinking=s.llm_enable_thinking,
        ) if s.llm_base_url else None
        # KB 按 kb_provider 路由（未配置凭据返回 None → 降级基础自检；
        # 未知 provider 抛 KbError，由 cli 顶层转退出码 6）
        kb = build_kb(s)

    local = LocalGitAdapter()

    # 本地仓库直连适配器（无需 Token，读 .git）注册为唯一 Git 适配器
    container = Container(git=local, llm=llm, kb=kb)
    return container


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


def select_git_adapter(platform: str) -> GitPlatformAdapter:
    """按平台选择 Git 适配器；当前仅支持本地（local）。"""
    return get_container().git_adapters[Platform.LOCAL.value]
