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
        fallback = home / ".config" if kind == "config" else home / ".local" / "state"
    base = os.environ.get(env_key)
    return (Path(base) if base else fallback) / "pr-check"


USER_CONFIG_DIR = _user_dir("config")
USER_STATE_DIR = _user_dir("state")
USER_ENV_FILE = USER_CONFIG_DIR / ".env"
LEGACY_CWD_DB = Path("pr_check.db")

# `pr-check config init` 的 .env 模板。装机形态（wheel）不随包附带仓库根的
# `.env.example`，模板必须内嵌于代码；tests/test_config_paths.py 有同步断言，
# 改动 `.env.example` 时必须同步此处，防止两份漂移。
ENV_TEMPLATE = """\
# ===== 应用基础 =====
APP_ENV=dev
# SQLite 数据库路径（存 KB 文档元数据）
# 留空（注释掉）则默认落到用户状态目录：
#   Windows: %LOCALAPPDATA%\\pr-check\\pr_check.db
#   Linux/macOS: $XDG_STATE_HOME/pr-check/pr_check.db（默认 ~/.local/state/pr-check/...）
# 这样装机形态下换目录执行也共用同一份 KB 元数据；想随仓库走再显式写相对/绝对路径。
# DATABASE_URL=sqlite:///./pr_check.db

# ===== LLM（OpenAI 兼容端点） =====
LLM_BASE_URL=
LLM_MODEL=
LLM_API_KEY=
# 单次请求超时（秒），默认 120
LLM_TIMEOUT_SECONDS=120
LLM_MAX_RETRIES=1
# 推理模型是否输出思维链：留空=不发送该字段（兼容不支持的端点）；
# false=关闭（硅基流动 Qwen3.5 实测 41s/2179 tokens → 1.1s/32 tokens）
LLM_ENABLE_THINKING=

# ===== 知识库（向量检索服务） =====
# KB_PROVIDER：maas（默认，MaaS Vector KB）/ openai（通用 OpenAI 风格向量检索）
# / dify（Dify 知识库，KB_INDEX 承载 dataset_id）
KB_PROVIDER=maas
KB_BASE_URL=
KB_API_KEY=
KB_INDEX=

# ===== 可配置阈值（可选，留空用默认值） =====
SMALL_MAX_FILES=20
SMALL_MAX_LINES=800
MEDIUM_MAX_FILES=80
MEDIUM_MAX_LINES=3000
KB_TOP_K=5

# ===== 调试（shell 环境变量，不写入本文件由 Settings 读取） =====
# PR_CHECK_DEBUG=1（true/yes/on 亦可）：pr_check logger 输出 DEBUG 日志到 stderr，
# 用于排查降级原因（KB 失败、stale 过滤跳过）与适配器异常类型；不影响 stdout 结构化输出。
"""


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
    # 知识库供应商：maas（默认，向后兼容）/ openai（通用 OpenAI 风格向量检索）/ dify
    kb_provider: str = "maas"

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
