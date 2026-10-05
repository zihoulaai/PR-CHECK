"""配置中心：环境变量分层 + 可配置阈值 / Top-K。

环境变量：APP_ENV / DATABASE_URL / LLM_* / KB_*。
三档 PR 阈值与 KB Top-K 可配置，便于调参。
"""
from __future__ import annotations

from functools import lru_cache
import os

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根（app/ 的上一级）。git 钩子等场景下 cwd 是被检仓库，读不到 cwd 的 .env，
# 因此把项目自带 .env 也作为来源；cwd 的 .env 仍放在后面，保持优先。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Settings(BaseSettings):
    # 注意：pydantic-settings 按字段名做大小写不敏感的环境变量匹配，
    # 例如 LLM_BASE_URL -> llm_base_url；因此不要给字段加 alias（会导致匹配失败）。
    model_config = SettingsConfigDict(
        env_file=(os.path.join(_PROJECT_ROOT, ".env"), ".env"),
        env_file_encoding="utf-8", extra="ignore", populate_by_name=True,
    )

    app_env: str = "dev"
    database_url: str = "sqlite:///./pr_check.db"

    # LLM
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = None
    llm_timeout_seconds: int = 120
    # 总尝试次数（含首次）。1 = 不重试；仅对 429 / 408 / 5xx 与网络层异常生效，
    # 其余 4xx 直接失败。调大前请确认上游配额可承受。
    llm_max_retries: int = 1
    # 是否让推理模型输出思维链（enable_thinking）。None = 不向端点发送该字段，
    # 兼容不支持它的端点；False 可显著降低延迟与 token（实测 41s/2179 tok → 1.1s/32 tok）。
    llm_enable_thinking: bool | None = None

    @field_validator("llm_enable_thinking", mode="before")
    @classmethod
    def _blank_to_none(cls, v):
        """.env 里留空（LLM_ENABLE_THINKING=）视为“不发送该字段”，避免空串解析失败。"""
        return None if isinstance(v, str) and not v.strip() else v

    # KB
    kb_base_url: str | None = None
    kb_api_key: str | None = None
    kb_index: str | None = None

    # 阈值（D7）
    small_max_files: int = 20
    small_max_lines: int = 800
    medium_max_files: int = 80
    medium_max_lines: int = 3000

    # KB（D8）
    kb_top_k: int = 5


@lru_cache
def get_settings() -> Settings:
    return Settings()


def is_llm_configured(s: Settings | None = None) -> bool:
    s = s or get_settings()
    return bool(s.llm_base_url and s.llm_model and s.llm_api_key)


def is_kb_configured(s: Settings | None = None) -> bool:
    s = s or get_settings()
    return bool(s.kb_base_url and s.kb_api_key)
