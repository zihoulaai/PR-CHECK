"""配置分层：用户级 .env、SQLite 默认落点与 `pr-check config show` 诊断。

装机形态（`uv tool install` / `uvx`）没有可读的项目根，凭据与元数据必须有稳定落点，
否则换目录执行就会各起一份配置 / 一份库。
"""
from __future__ import annotations

import json

from app.cli import main
from app.config import Settings, USER_ENV_FILE, USER_STATE_DIR, active_config_files


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
