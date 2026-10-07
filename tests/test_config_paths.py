"""配置分层：用户级 .env、SQLite 默认落点与 `pr-check config show` 诊断。

装机形态（`uv tool install` / `uvx`）没有可读的项目根，凭据与元数据必须有稳定落点，
否则换目录执行就会各起一份配置 / 一份库。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.cli import main
from app.config import (
    ENV_TEMPLATE,
    Settings,
    USER_ENV_FILE,
    USER_STATE_DIR,
    _PROJECT_ROOT,
    active_config_files,
)
from app.errors import NotConfiguredError

# prod 强制校验的五个必填环境变量（测试按名断言错误提示）
_PROD_REQUIRED_ENV = (
    "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY",
    "KB_BASE_URL", "KB_API_KEY",
)
# 与 _PROD_REQUIRED_ENV 对应的 Settings 构造参数
_FULL_CREDS = dict(
    llm_base_url="https://llm.example.invalid/v1",
    llm_model="test-model",
    llm_api_key="test-llm-key",
    kb_base_url="https://kb.example.invalid",
    kb_api_key="test-kb-key",
)


def test_default_database_is_user_scoped():
    """默认库落在用户状态目录，而不是 cwd（旧行为会随 cwd 漂移）。"""
    default = Settings.model_fields["database_url"].default
    assert default.startswith("sqlite:///")
    assert "pr-check/pr_check.db" in default
    assert not default.startswith("sqlite:///./")


def test_user_env_file_is_a_config_source():
    """用户级 .env 参与配置来源，且优先级低于当前目录 .env。"""
    assert str(USER_ENV_FILE) in Settings.model_config["env_file"]
    sources = list(Settings.model_config["env_file"])
    assert sources.index(str(USER_ENV_FILE)) < sources.index(".env")


def test_active_config_files_filters_missing():
    files = active_config_files()
    assert isinstance(files, list)
    assert all(f for f in files)  # 只返回真实存在的文件


def test_config_show_reports_paths_and_flags(capsys):
    """config show：给出配置来源清单、数据库路径与 LLM/KB 配置状态。"""
    assert main(["config", "show"]) == 0
    payload = json.loads(capsys.readouterr().out)
    for key in ("config_files", "user_env_file", "database_path", "app_env",
                "llm_configured", "kb_configured"):
        assert key in payload
    assert isinstance(payload["llm_configured"], bool)
    assert isinstance(payload["kb_configured"], bool)
    assert USER_STATE_DIR.name in payload["user_env_file"] or "pr-check" in payload["user_env_file"]


# ===== P3 config init =====
def test_config_init_writes_template(capsys, tmp_path):
    """init --path：按内嵌模板生成 .env，内容与 ENV_TEMPLATE 完全一致。"""
    target = tmp_path / "conf" / ".env"
    assert main(["config", "init", "--path", str(target)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] is True
    assert payload["overwritten"] is False
    assert Path(payload["path"]) == target
    assert target.read_text(encoding="utf-8") == ENV_TEMPLATE


def test_config_init_creates_parent_dirs(capsys, tmp_path):
    """目标父目录不存在时自动创建（用户级目录首次装机场景）。"""
    target = tmp_path / "deep" / "nested" / ".env"
    assert main(["config", "init", "--path", str(target)]) == 0
    capsys.readouterr()
    assert target.is_file()


def test_config_init_refuses_existing_without_force(capsys, tmp_path):
    """目标已存在且未传 --force：INVALID_REQUEST（rc 2），原文件一字不动。"""
    target = tmp_path / ".env"
    target.write_text("LLM_API_KEY=已填好的密钥\n", encoding="utf-8")
    assert main(["config", "init", "--path", str(target)]) == 2
    err = json.loads(capsys.readouterr().out)
    assert err["error"]["code"] == "INVALID_REQUEST"
    assert "--force" in err["error"]["message"]
    assert target.read_text(encoding="utf-8") == "LLM_API_KEY=已填好的密钥\n"


def test_config_init_force_overwrites(capsys, tmp_path):
    """--force：显式覆盖已存在的目标文件，payload 标记 overwritten。"""
    target = tmp_path / ".env"
    target.write_text("旧内容\n", encoding="utf-8")
    assert main(["config", "init", "--path", str(target), "--force"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["overwritten"] is True
    assert target.read_text(encoding="utf-8") == ENV_TEMPLATE


def test_env_template_matches_repo_example():
    """内嵌模板必须与仓库根 `.env.example` 逐字节一致（防漂移；wheel 形态无该文件则跳过）。"""
    example = Path(_PROJECT_ROOT) / ".env.example"
    if not example.is_file():
        pytest.skip("非源码树：wheel 形态不附带 .env.example，无同步基准")
    assert ENV_TEMPLATE == example.read_text(encoding="utf-8")


# ===== APP_ENV=prod 必填凭据强制校验 =====
def test_dev_allows_missing_credentials():
    """dev（默认）：凭据全部缺失也能构造，保持离线/降级运行能力。"""
    s = Settings(_env_file=None, app_env="dev")
    assert s.llm_api_key is None
    assert s.kb_api_key is None


@pytest.mark.parametrize("app_env", ["dev", "test", "staging", ""])
def test_non_prod_envs_not_validated(app_env):
    """非生产环境值（含空串）不触发强校验。"""
    assert Settings(_env_file=None, app_env=app_env).app_env == app_env


def test_prod_missing_all_credentials_lists_every_var():
    """prod 且五项全缺：拒绝构造，错误一次性列出全部缺失变量名。"""
    with pytest.raises(PydanticValidationError) as exc:
        Settings(_env_file=None, app_env="prod")
    message = str(exc.value)
    for name in _PROD_REQUIRED_ENV:
        assert name in message


def test_prod_reports_only_missing_items():
    """prod 且仅缺 KB_API_KEY：错误只点名 KB_API_KEY，不牵连已配置项。"""
    with pytest.raises(PydanticValidationError) as exc:
        Settings(_env_file=None, app_env="prod", **{**_FULL_CREDS, "kb_api_key": "  "})
    message = str(exc.value)
    assert "KB_API_KEY" in message
    for name in ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "KB_BASE_URL"):
        assert name not in message


def test_prod_passes_when_all_credentials_present():
    """prod 且五项齐全（production 别名）：正常构造。"""
    s = Settings(_env_file=None, app_env="production", **_FULL_CREDS)
    assert s.app_env == "production"


@pytest.mark.parametrize("app_env", ["prod", "PROD", "Prod", " production ", "Production"])
def test_prod_env_name_matching(app_env):
    """prod/production 大小写不敏感、忽略首尾空白，均触发强校验。"""
    with pytest.raises(PydanticValidationError):
        Settings(_env_file=None, app_env=app_env)


@pytest.fixture
def prod_env(monkeypatch):
    """APP_ENV=prod 且五项必填均注入哨兵值（环境变量优先级高于 .env，隔绝本机真实配置）。

    用例可把某一项 setenv 成 "" 模拟该项缺失；前后均清 get_settings 缓存。
    """
    from app.config import get_settings

    env = {"APP_ENV": "prod", **{
        "LLM_BASE_URL": _FULL_CREDS["llm_base_url"],
        "LLM_MODEL": _FULL_CREDS["llm_model"],
        "LLM_API_KEY": _FULL_CREDS["llm_api_key"],
        "KB_BASE_URL": _FULL_CREDS["kb_base_url"],
        "KB_API_KEY": _FULL_CREDS["kb_api_key"],
    }}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def test_get_settings_prod_missing_raises_not_configured(prod_env):
    """get_settings 把 prod 校验失败转成 NOT_CONFIGURED 业务错误（而非 pydantic 堆栈）。"""
    from app.config import get_settings

    prod_env.setenv("KB_API_KEY", "")
    with pytest.raises(NotConfiguredError) as exc:
        get_settings()
    assert exc.value.code == "NOT_CONFIGURED"
    assert "KB_API_KEY" in str(exc.value)


def test_cli_config_show_prod_missing_returns_rc3(prod_env, capsys):
    """CLI 任意命令加载配置即拦截：config show 在 prod 缺凭据时返回 rc 3 错误信封。"""
    prod_env.setenv("LLM_API_KEY", "")
    assert main(["config", "show"]) == 3
    body = json.loads(capsys.readouterr().out)
    assert body["error"]["code"] == "NOT_CONFIGURED"
    assert "LLM_API_KEY" in body["error"]["message"]


def test_cli_config_show_prod_configured_ok(prod_env, capsys):
    """prod 五项齐全时 config show 正常输出，并标记 LLM/KB 均已配置。"""
    assert main(["config", "show"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["app_env"] == "prod"
    assert payload["llm_configured"] is True
    assert payload["kb_configured"] is True
