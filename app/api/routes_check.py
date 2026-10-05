"""Check 路由（C1 / C12）。

POST /check：两种定位方式（浏览选择 / 手工输入）归一为 ProjectRef + MRRef。
成功返回 {report, rendered_markdown}。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.adapters.base import GitCredential
from app.api.deps import resolve_git_credential
from app.agent.workflow import run_check
from app.domain.schemas import CheckRequest, CheckResponse, MRRef, ProjectRef
from app.report.markdown import render_markdown

router = APIRouter(prefix="/check", tags=["check"])


@router.post("", response_model=CheckResponse)
def check(req: CheckRequest,
          cred: GitCredential = Depends(resolve_git_credential)):
    project = ProjectRef(id=req.project_id, path=req.project_path)
    mr_ref = MRRef(project=project, iid=req.mr_iid)
    report = run_check(cred, mr_ref)
    rendered = render_markdown(report)
    return CheckResponse(report=report, rendered_markdown=rendered)
