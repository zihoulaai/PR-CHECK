"""远端 Git 平台适配器共享的 HTTP 层（R4）。

GitHub / GitLab 适配器共享「Token 校验 + 状态码映射 + 网络降级」逻辑，集中在此
避免两份漂移。状态码语义与本地适配器一致：统一抛 GitPlatformError 子类，
由 CLI 映射到退出码 4（见 app/cli._CODE_TO_EXIT）。

只读：本模块只提供 GET 请求入口。D5 明确 R4 不做任何写操作。
"""
from __future__ import annotations

import httpx

from app.errors import (
    GitAuthFailed,
    GitForbidden,
    GitUnavailable,
    ProjectNotFound,
)


def require_token(token: str, platform_label: str) -> str:
    """远端平台必须有 Token；缺失即抛 GitAuthFailed（退出码 4），不静默发匿名请求。"""
    token = (token or "").strip()
    if not token:
        raise GitAuthFailed(
            f"{platform_label} 需要访问 Token（check --git-token 或配置 GIT_TOKEN）。"
        )
    return token


def get(url: str, *, headers: dict, params: dict | None = None, timeout: int = 30,
        not_found: type[Exception] = ProjectNotFound):
    """发起只读 GET 请求，返回 httpx.Response；错误按状态码映射为 GitPlatformError。

    - 401 → GitAuthFailed（Token 无效）
    - 403 → GitForbidden（权限不足 / 配额）
    - 404 → not_found（项目或 MR 不存在，由调用方指定具体类型）
    - 其余 >=400 → GitUnavailable
    - 网络层异常 → GitUnavailable（不泄露 URL / Token）
    """
    try:
        with httpx.Client(timeout=timeout) as c:
            resp = c.get(url, headers=headers, params=params)
    except httpx.TimeoutException as exc:
        raise GitUnavailable(f"Git 平台请求超时：{type(exc).__name__}") from exc
    except httpx.HTTPError as exc:
        raise GitUnavailable(f"Git 平台网络不可达：{type(exc).__name__}") from exc

    if resp.status_code == 401:
        raise GitAuthFailed()
    if resp.status_code == 403:
        raise GitForbidden()
    if resp.status_code == 404:
        raise not_found()
    if resp.status_code >= 400:
        raise GitUnavailable(f"Git 平台返回错误状态码 {resp.status_code}。")
    return resp


def json_body(resp) -> object:
    """解析响应 JSON；响应体非 JSON 时转 GitUnavailable（避免裸异常冒泡成 rc=99）。"""
    try:
        return resp.json()
    except ValueError as exc:
        raise GitUnavailable("Git 平台响应体非 JSON。") from exc


def project_path(proj) -> str:
    """取远端项目路径（GitHub owner/repo、GitLab group/project）。缺失即报错。"""
    path = (getattr(proj, "path", "") or "").strip().strip("/")
    if not path:
        raise ProjectNotFound("远端平台需要项目路径（owner/repo 或 group/project）。")
    return path
