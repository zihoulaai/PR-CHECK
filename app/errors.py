"""统一错误类型。

所有对外错误统一格式（C1）：
    {"error": {"code": "...", "message": "..."}}
内部异常 / 堆栈 / Token 等不得暴露（M5 / S3）。

CLI 通过 AppError.to_body() 产出同样的信封，配合退出码返回（见 bin/pr_check_cli.py）。
"""
from __future__ import annotations

import logging

logger = logging.getLogger("pr_check")


class AppError(Exception):
    """业务错误基类：携带对外安全错误码与中文提示。"""

    code: str = "INTERNAL_ERROR"
    status_code: int = 500
    friendly_message: str = "服务暂时不可用，请稍后重试。"

    def __init__(self, message: str | None = None, *, code: str | None = None):
        self.code = code or self.code
        super().__init__(message or self.friendly_message)

    def to_body(self) -> dict:
        return {"error": {"code": self.code, "message": str(self.args[0] or self.friendly_message)}}


# ===== Git（本地自检） =====
class GitPlatformError(AppError):
    """Git 错误基类（平台中立）；status 502；不泄露 Token / 堆栈。"""

    status_code = 502


class GitUnavailable(GitPlatformError):
    code = "GIT_UNAVAILABLE"
    friendly_message = "无法获取 PR 变更，请检查本地 Git 仓库与分支。"


class GitAuthFailed(GitPlatformError):
    code = "GIT_AUTH_FAILED"
    friendly_message = "Git 鉴权失败，请检查本地仓库权限。"


class GitForbidden(GitPlatformError):
    code = "GIT_FORBIDDEN"
    friendly_message = "无权访问该仓库，请确认仓库可见性。"


class ProjectNotFound(GitPlatformError):
    code = "PROJECT_NOT_FOUND"
    friendly_message = "未找到对应的项目，请确认项目路径。"


class MrNotFound(GitPlatformError):
    code = "MR_NOT_FOUND"
    friendly_message = "未找到对应的 MR，请确认分支状态。"


# ===== KB =====
class KbError(AppError):
    code = "KB_UNAVAILABLE"
    status_code = 502
    friendly_message = "知识库检索暂时不可用，本次已降级为基础自检。"


# ===== LLM（M5） =====
class LlmError(AppError):
    status_code = 502


class LlmUnavailable(LlmError):
    code = "LLM_UNAVAILABLE"
    friendly_message = "自检服务暂时不可用，请稍后重试。"


class LlmTimeout(LlmError):
    code = "LLM_TIMEOUT"
    friendly_message = "自检服务响应超时，请稍后重试。"


class LlmRateLimited(LlmError):
    code = "LLM_RATE_LIMITED"
    friendly_message = "自检服务请求过于频繁，请稍后重试。"


class LlmInvalidOutput(LlmError):
    code = "LLM_INVALID_OUTPUT"
    friendly_message = "自检结果格式异常，请稍后重试。"


# ===== 其它 =====
class NotConfiguredError(AppError):
    code = "NOT_CONFIGURED"
    status_code = 400
    friendly_message = "相关服务尚未配置，请先在连接设置中完成配置。"


class ValidationError(AppError):
    code = "INVALID_REQUEST"
    status_code = 400


class SecurityError(AppError):
    code = "SECURITY_ERROR"
    status_code = 500
    friendly_message = "安全相关操作失败，请检查服务端配置。"
