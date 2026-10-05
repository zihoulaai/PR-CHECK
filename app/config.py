"""配置中心：环境变量分层 + 可配置阈值 / Top-K。

依据 docs/IMPLEMENTATION-READY-v1.md C2：
- 环境变量：APP_ENV / APP_ENCRYPTION_KEY / DATABASE_URL / LLM_* / KB_*
- GitLab 连接（含加密 Token）存 SQLite，不在此处。
- 三档 PR 阈值与 KB Top-K 可配置，便于调参（D7/D8）。
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # 注意：pydantic-settings 按字段名做大小写不敏感的环境变量匹配，
    # 例如 LLM_BASE_URL -> llm_base_url；因此不要给字段加 alias（会导致匹配失败）。
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True
    )

    app_env: str = "dev"
    app_encryption_key: str | None = None
    database_url: str = "sqlite:///./pr_check.db"

    # LLM
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = None
    llm_timeout_seconds: int = 60
    llm_max_retries: int = 1

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
