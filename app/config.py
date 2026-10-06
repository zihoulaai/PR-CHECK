"""配置中心：环境变量分层 + 可配置阈值 / Top-K。

环境变量：APP_ENV / DATABASE_URL / LLM_* / KB_*。
三档 PR 阈值与 KB Top-K 可配置，便于调参。
"""
from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根（app/ 的上一级）。git 钩子等场景下 cwd 是被检仓库，读不到 cwd 的 .env，
# 因此把项目自带 .env 也作为来源；cwd 的 .env 仍放在最后，保持最高优先级。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _user_dir(kind: str) -> Path:
    """用户级目录：config 存凭据（.env），state 存 SQLite 元数据。

    Windows 走 APPDATA / LOCALAPPDATA，其余平台跟随 XDG_*。
    装机形态（`uv tool install` / `uvx`）没有可读的项目根，凭据与数据必须有稳定落点。
    """
    home = Path.home()
    if os.name == "nt":
        env_key = "APPDATA" if kind == "config" else "LOCALAPPDATA"
        fallback = home / "AppData" / ("Roaming" if kind == "config" else "Local")
    else:
        env_key = "XDG_CONFIG_HOME" if kind == "config" else "XDG_STATE_HOME"
        fallback = home / (".config" if kind == "config" else ".local" / "state")
    base = os.environ.get(env_key)
    return (Path(base) if base else fallback) / "pr-check"


USER_CONFIG_DIR = _user_dir("config")
USER_STATE_DIR = _user_dir("state")
USER_ENV_FILE = USER_CONFIG_DIR / ".env"
LEGACY_CWD_DB = Path("pr_check.db")


class Settings(BaseSettings):
    # 注意：pydantic-settings 按字段名做大小写不敏感的环境变量匹配，
    # 例如 LLM_BASE_URL -> llm_base_url；因此不要给字段加 alias（会导致匹配失败）。
    model_config = SettingsConfigDict(
        env_file=(
            os.path.join(_PROJECT_ROOT, ".env"),  # 包/源码目录（源码形态）
            str(USER_ENV_FILE),                   # 用户级（装机形态推荐落点）
            ".env",                               # 当前目录，优先级最高
        ),
        env_file_encoding="utf-8", extra="ignore", populate_by_name=True,
    )

    app_env: str = "dev"
    # 默认落用户状态目录：装机形态下换仓库执行不会各起一份库（旧版默认 ./pr_check.db 会随 cwd 漂移）。
    database_url: str = f"sqlite:///{USER_STATE_DIR.joinpath('pr_check.db').as_posix()}"

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
    settings = Settings()
    try:
        # SQLite 不会自建父目录，缺省路径下需先兜住（自定义 DATABASE_URL 时这一步无副作用）。
        USER_STATE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:  # 只读 HOME 等异常环境：交给 SQLite 报错更直观
        pass
    return settings


def active_config_files() -> list[str]:
    """返回按优先级升序实际存在的配置文件路径，供 `pr-check config show` 排查来源。"""
    return [p for p in (
        os.path.join(_PROJECT_ROOT, ".env"), str(USER_ENV_FILE), ".env",
    ) if os.path.isfile(p)]


def database_path(settings: Settings | None = None) -> str:
    """从 sqlite URL 还原数据库文件路径（仅用于诊断展示）。"""
    url = (settings or get_settings()).database_url
    if url.startswith("sqlite:///"):
        return url[len("sqlite:///"):]
    return url


def is_llm_configured(s: Settings | None = None) -> bool:
    s = s or get_settings()
    return bool(s.llm_base_url and s.llm_model and s.llm_api_key)


def is_kb_configured(s: Settings | None = None) -> bool:
    s = s or get_settings()
    return bool(s.kb_base_url and s.kb_api_key)
