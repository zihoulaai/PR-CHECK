"""配置分层：用户级 .env、SQLite 默认落点与 `pr-check config show` 诊断。

装机形态（`uv tool install` / `uvx`）没有可读的项目根，凭据与元数据必须有稳定落点，
否则换目录执行就会各起一份配置 / 一份库。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.cli import main
from app.config import (
    ENV_TEMPLATE,
    Settings,
    USER_ENV_FILE,
    USER_STATE_DIR,
    _PROJECT_ROOT,
    active_config_files,
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
