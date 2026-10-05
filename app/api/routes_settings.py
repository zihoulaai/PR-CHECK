"""Settings 路由（C1）。

GET /settings：返回非敏感连接配置（禁止返回 Token / API Key / Secret）。
POST /settings/gitlab：保存 GitLab 连接，Token 加密存储、不回显。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.config import get_settings, is_kb_configured, is_llm_configured
from app.domain.schemas import SettingsGitLabIn, SettingsGitLabOut
from app.errors import NotConfiguredError, SecurityError
from app.security.secrets import get_secret_store
from app.storage.repo import list_git_connections, upsert_git_connection

router = APIRouter(prefix="/settings", tags=["settings"])


class _SettingsOut(BaseModel):
    gitlab: dict
    llm: dict
    kb: dict


@router.get("", response_model=_SettingsOut)
def get_settings_view():
    s = get_settings()
    conns = list_git_connections()
    gitlab_cfg = bool(conns and any(c.encrypted_token for c in conns))
    return _SettingsOut(
        gitlab={
            "configured": gitlab_cfg,
            "connections": [
                {"id": c.id, "name": c.name, "base_url": c.base_url} for c in conns
            ],
        },
        llm={"configured": is_llm_configured(s), "model": s.llm_model or ""},
        kb={"configured": is_kb_configured(s)},
    )


@router.post("/gitlab", response_model=SettingsGitLabOut)
def save_gitlab(inp: SettingsGitLabIn):
    try:
        store = get_secret_store()
    except SecurityError:
        raise NotConfiguredError("服务端未配置主密钥，无法保存连接凭据。")
    encrypted = store.encrypt(inp.token)
    conn = upsert_git_connection(name=inp.name, base_url=inp.base_url,
                                 encrypted_token=encrypted)
    # 不回显 token
    return SettingsGitLabOut(id=conn.id, name=conn.name, base_url=conn.base_url)
