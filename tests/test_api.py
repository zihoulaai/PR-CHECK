"""REST 契约与错误格式测试（C1 / 统一错误格式）。"""
from __future__ import annotations


def test_get_settings(client):
    r = client.get("/settings")
    assert r.status_code == 200
    body = r.json()
    assert "gitlab" in body and "llm" in body and "kb" in body
    # 不得返回 token / key
    assert "token" not in str(body).lower() or "configured" in str(body)


def test_post_gitlab_settings(client):
    r = client.post("/settings/gitlab", json={
        "name": "测试 GitLab", "base_url": "https://gitlab.example.com",
        "token": "glpat-xxxx",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "gitlab-default"
    assert "token" not in body
    assert body["base_url"] == "https://gitlab.example.com"


def test_list_projects_and_mrs(client):
    r = client.get("/projects")
    assert r.status_code == 200
    assert r.json()["items"]
    r2 = client.get("/projects/123/mrs")
    assert r2.status_code == 200
    assert r2.json()["items"]


def test_post_check(client):
    r = client.post("/check", json={"project_id": 123, "mr_iid": 1234})
    assert r.status_code == 200
    body = r.json()
    assert "report" in body and "rendered_markdown" in body
    assert "PR 提交前置自检报告" in body["rendered_markdown"]
    assert body["report"]["meta"]["project"]


def test_kb_upload_and_list(client):
    r = client.post("/kb/docs", data={
        "project": "order-service", "module": "refund",
        "doc_type": "technical_debt", "title": "退款缓存问题",
    }, files={"file": ("refund.txt", b"refund cache inconsistency")})
    assert r.status_code == 200
    doc_id = r.json()["id"]
    assert doc_id.startswith("kb-")
    lst = client.get("/kb/docs", params={"project": "order-service"})
    assert lst.status_code == 200
    assert any(d["id"] == doc_id for d in lst.json()["items"])


def test_invalid_check_request_format(client):
    # 先配置 GitLab，避免依赖层先报错
    client.post("/settings/gitlab", json={
        "name": "测试 GitLab", "base_url": "https://gitlab.example.com", "token": "t"})
    # mr_iid 缺失 -> 统一错误格式（422 + {error:{code,message}}）
    r = client.post("/check", json={"project_id": 123})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_REQUEST"
