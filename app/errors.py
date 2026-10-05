"""统一错误类型与 HTTP 错误映射。

所有接口返回统一格式（C1）：
    {"error": {"code": "...", "message": "..."}}
内部异常 / 堆栈 / Token 等不得暴露（M5 / S3）。
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
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


# ===== GitLab（M5） =====
class GitLabError(AppError):
    status_code = 502


class GitLabUnavailable(GitLabError):
    code = "GITLAB_UNAVAILABLE"
    friendly_message = "无法获取 PR 变更，请检查 GitLab 连接配置及项目权限。"


class GitLabAuthFailed(GitLabError):
    code = "GITLAB_AUTH_FAILED"
    friendly_message = "GitLab 鉴权失败，请检查连接配置中的 Token。"


class GitLabForbidden(GitLabError):
    code = "GITLAB_FORBIDDEN"
    friendly_message = "无权访问该项目或 MR，请确认 Token 权限与项目可见性。"


class ProjectNotFound(GitLabError):
    code = "PROJECT_NOT_FOUND"
    friendly_message = "未找到对应的项目，请确认项目路径或连接配置。"


class MrNotFound(GitLabError):
    code = "MR_NOT_FOUND"
    friendly_message = "未找到对应的 MR，请确认 MR 编号或分支状态。"


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


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        # 仅记录安全字段，不记录内部细节 / Token / 堆栈
        logger.error("app_error code=%s", exc.code)
        return JSONResponse(status_code=exc.status_code, content=exc.to_body())

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "INVALID_REQUEST", "message": "请求参数校验失败。"}},
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.error("unexpected_error type=%s", type(exc).__name__)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "INTERNAL_ERROR", "message": "服务内部错误，请稍后重试。"}},
        )
