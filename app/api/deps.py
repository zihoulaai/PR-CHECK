"""路由依赖：从 SQLite 连接解析并解密 GitLab 凭据（D14 / S3）。

Token 仅在调用 Adapter 的瞬间解密于内存，不落日志 / 不进 LLM / 不进报告。
"""
from __future__ import annotations

from fastapi import Query

from app.adapters.base import GitCredential
from app.errors import GitLabAuthFailed, NotConfiguredError
from app.security.secrets import get_secret_store
from app.storage.repo import get_git_connection


def resolve_git_credential(connection_id: str | None = Query(default=None)) -> GitCredential:
    cid = connection_id or "gitlab-default"
    conn = get_git_connection(cid)
    if conn is None or not conn.encrypted_token:
        raise NotConfiguredError("GitLab 连接未配置，请先在设置中完成配置。")
    try:
        token = get_secret_store().decrypt(conn.encrypted_token)
    except Exception as exc:
        raise GitLabAuthFailed("GitLab 连接凭据解密失败，请重新配置。") from exc
    if not token:
        raise GitLabAuthFailed("GitLab Token 为空。")
    return GitCredential(base_url=conn.base_url, token=token)
