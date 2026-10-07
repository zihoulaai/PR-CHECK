"""配置中心：环境变量分层 + 可配置阈值 / Top-K。

环境变量：APP_ENV / DATABASE_URL / LLM_* / KB_*。
三档 PR 阈值与 KB Top-K 可配置，便于调参。

APP_ENV=prod / production 时启动强制凭据校验：LLM 三件套与 KB 凭据必须全部配齐，
缺失即在配置加载阶段报 NOT_CONFIGURED，避免生产环境静默降级、闸门永不触发。
"""
from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.errors import NotConfiguredError

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

# 触发生产环境强校验的 APP_ENV 取值（大小写不敏感、忽略首尾空白）。
PROD_APP_ENVS = frozenset({"prod", "production"})

# APP_ENV=prod 时必须配齐的凭据（Settings 字段名 -> .env 变量名）。
# LLM 是自检的硬依赖；KB 在 dev 下允许缺省并降级为基础自检，但 prod 下闸门
# risk:high / rule:violation 依赖 A/B 级 KB 证据，缺凭据会静默永不触发，
# 构成「以为受保护」的信任陷阱，因此同样强制。
PROD_REQUIRED_FIELDS: dict[str, str] = {
    "llm_base_url": "LLM_BASE_URL",
    "llm_model": "LLM_MODEL",
    "llm_api_key": "LLM_API_KEY",
    "kb_base_url": "KB_BASE_URL",
    "kb_api_key": "KB_API_KEY",
}


def missing_prod_credentials(s: "Settings") -> list[str]:
    """返回 prod/production 环境下缺失（未填或为空白）的必填变量名；其余环境返回 []。"""
    if (s.app_env or "").strip().lower() not in PROD_APP_ENVS:
        return []
    return [
        env_name
        for field_name, env_name in PROD_REQUIRED_FIELDS.items()
        if not str(getattr(s, field_name) or "").strip()
    ]


# `pr-check config init` 的 .env 模板。装机形态（wheel）不随包附带仓库根的
# `.env.example`，模板必须内嵌于代码；tests/test_config_paths.py 有同步断言，
# 改动 `.env.example` 时必须同步此处，防止两份漂移。
ENV_TEMPLATE = """\
# ===== 应用基础 =====
# 运行环境：dev（默认，凭据缺失时允许离线/降级运行）；设为 prod / production 时
# 启动强制校验——LLM_BASE_URL、LLM_MODEL、LLM_API_KEY、KB_BASE_URL、KB_API_KEY
# 必须全部配齐，缺失即拒绝运行（NOT_CONFIGURED），避免生产环境静默降级、闸门失效。
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

    @model_validator(mode="after")
    def _prod_requires_credentials(self) -> "Settings":
        """prod/production：LLM 三件套 + KB 凭据缺一即拒绝启动（一次性列出全部缺项）。"""
        missing = missing_prod_credentials(self)
        if missing:
            raise ValueError(
                "APP_ENV=prod 为生产环境，以下必填配置缺失或为空："
                + "、".join(missing)
                + "。请在 .env 中补全后重试（可用 `pr-check config init` 生成模板，"
                "`pr-check config show` 查看配置来源）。"
            )
        return self


def _settings_error_message(exc: PydanticValidationError) -> str:
    """提取配置校验错误的人类可读信息（优先取 model_validator 抛出的原始 ValueError）。"""
    parts: list[str] = []
    for err in exc.errors():
        cause = (err.get("ctx") or {}).get("error")
        parts.append(str(cause) if cause else err.get("msg", "配置校验失败。"))
    return "；".join(parts) or "配置校验失败。"


@lru_cache
def get_settings() -> Settings:
    try:
        settings = Settings()
    except PydanticValidationError as exc:
        # 配置层校验（prod 必填凭据等）转成统一业务错误：CLI 顶层映射
        # NOT_CONFIGURED（rc=3），不向用户暴露 pydantic 原始堆栈。
        raise NotConfiguredError(_settings_error_message(exc)) from exc
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
