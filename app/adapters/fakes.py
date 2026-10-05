"""离线 Mock 适配器：无凭据即可跑通后端与测试（用户已确认方案）。

- FakeGitLab：确定性返回样例项目 / MR / Diff，便于解析与画像测试。
- FakeLLM：返回合法 CheckReport 分段 JSON（meta 由 workflow 构造）。
- FakeKB：内存版向量库，支持按 project 强制过滤。
"""
from __future__ import annotations

import json
import uuid

from app.adapters.base import (
    GitCredential,
    GitPlatformAdapter,
    KbDocInput,
    LLMClient,
    MRItem,
    ProjectItem,
)
from app.domain.schemas import KBHit, KBQuery, MRRef, PRMetadata, ProjectRef

DEFAULT_SAMPLE_DIFF = """diff --git a/src/refund/RefundController.java b/src/refund/RefundController.java
new file mode 100644
--- /dev/null
+++ b/src/refund/RefundController.java
@@ -0,0 +1,12 +1,12 @@
+package com.order.refund;
+
+@RestController
+@RequestMapping("/api/refund")
+public class RefundController {
+    @GetMapping("/{id}")
+    public RefundVO getRefund(long id) {
+        return service.find(id);
+    }
+}
diff --git a/src/main/resources/application.yml b/src/main/resources/application.yml
index 111..222 100644
--- a/src/main/resources/application.yml
+++ b/src/main/resources/application.yml
@@ -1,3 +1,4 @@
 server:
   port: 8080
+  ssl: enabled
"""


class FakeGitLab:
    def __init__(self, sample_diff: str = DEFAULT_SAMPLE_DIFF, project_id: int = 123,
                 project_path: str = "team/order-service"):
        self.sample_diff = sample_diff
        self.project_id = project_id
        self.project_path = project_path

    def list_projects(self, cred: GitCredential, *, search=None, page=1, per_page=20):
        return [ProjectItem(
            id=self.project_id, path=self.project_path.split("/")[-1],
            path_with_namespace=self.project_path, web_url="",
        )]

    def list_mrs(self, cred: GitCredential, proj: ProjectRef, *, state="opened",
                 page=1, per_page=20):
        return [MRItem(
            iid=1234, title="增加退款接口", source_branch="feature/refund",
            target_branch="main", updated_at="2026-10-05T12:00:00Z",
            author="developer",
        )]

    def get_mr(self, cred: GitCredential, ref: MRRef) -> PRMetadata:
        return PRMetadata(
            project=self.project_path, repository=self.project_path,
            pr_id=ref.iid, title="增加退款接口",
            description="支持订单部分退款", source_branch="feature/refund",
            target_branch="main", author="developer",
            updated_at="2026-10-05T12:00:00Z", web_url="",
        )

    def get_diff(self, cred: GitCredential, ref: MRRef) -> str:
        return self.sample_diff


class FakeLLM:
    def __init__(self, model: str = "fake-llm", report_override: dict | None = None):
        self.model = model
        self.report_override = report_override

    def complete(self, system: str, user: str) -> str:
        if self.report_override is not None:
            return json.dumps(self.report_override, ensure_ascii=False)
        sections = {
            "summary": "（离线 Mock）本次 PR 主要新增退款接口并调整了应用配置。",
            "doc_check": [
                {"item": "API文档", "verdict": "confirm", "basis": "检测到公共 API 新增",
                 "advice": "确认接口文档已同步", "evidence_level": "C", "source_refs": []}
            ],
            "risk": [
                {"level": "medium", "text": "新增公共 API，建议确认调用方兼容性",
                 "evidence_level": "C", "source_refs": []}
            ],
            "project_rules": [],
            "tech_debt": [],
            "manual_checklist": [
                "API兼容性", "测试覆盖", "异常和边界条件", "数据库迁移",
                "配置同步", "日志敏感信息", "文档同步",
            ],
        }
        return json.dumps(sections, ensure_ascii=False)


class FakeKB:
    def __init__(self):
        self._docs: list[KBHit] = []

    def add_doc(self, *, id: str, title: str, doc_type: str, module: str,
                project: str, snippet: str, score: float = 0.9) -> None:
        self._docs.append(KBHit(id=id, title=title, doc_type=doc_type,
                                module=module, project=project, snippet=snippet,
                                score=score))

    def search(self, query: KBQuery) -> list[KBHit]:
        if not query.project:
            raise RuntimeError("KBQuery.project 必填")
        hits = [h for h in self._docs if h.project == query.project]
        # 按 focus / doc_type 粗排
        if query.focus:
            hits = [h for h in hits if h.doc_type in query.focus] or hits
        return hits[:10]

    def upload(self, doc: KbDocInput) -> str:
        doc_id = f"kb-{uuid.uuid4().hex[:12]}"
        self._docs.append(KBHit(
            id=doc_id, title=doc.title, doc_type=doc.doc_type,
            module=doc.module, project=doc.project, snippet=doc.content[:200],
            score=1.0,
        ))
        return doc_id
