"""离线 Mock 适配器：无凭据即可跑通后端与测试（用户已确认方案）。

- FakeGitLab：确定性返回样例项目 / MR / Diff，便于解析与画像测试。
- FakeLLM：返回合法 CheckReport 分段 JSON（meta 由 workflow 构造）。
- FakeKB：内存版向量库，支持按 project 强制过滤。
"""
from __future__ import annotations

import json
import re
import uuid

from app.adapters.base import (
    GitCredential,
    KbDocInput,
    MRItem,
    ProjectItem,
)
from app.domain.enums import Platform
from app.domain.schemas import KBHit, KBQuery, MRRef, PRMetadata, ProjectRef
from app.errors import KbError

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


class FakeGitPlatform:
    """离线 Mock Git 适配器。

    行为与旧 FakeGitLab 一致，提供确定性样例项目 / MR / Diff，便于解析与画像测试。
    """

    def __init__(self, platform: Platform | str = Platform.LOCAL,
                 sample_diff: str = DEFAULT_SAMPLE_DIFF, project_id: int = 123,
                 project_path: str = "team/order-service",
                 sample_pr: dict | None = None):
        self.platform = Platform(platform)
        self.sample_diff = sample_diff
        self.project_id = project_id
        self.project_path = project_path
        # PR 元数据覆盖（project / title / description …）。
        # KB 检索按 project 强制过滤，pr.project 为空或与知识库不符时检索必然落空
        # （kb_status=empty）——评估用例必须能把 project 注入到工作流内部，
        # 否则只能在循环外另行构造 PRMetadata 跑检索，指标就与生产链路脱钩。
        self.sample_pr = dict(sample_pr or {})

    def list_projects(self, cred: GitCredential, *, search=None, page=1, per_page=20):
        return [ProjectItem(
            id=self.project_id, path=self.project_path.split("/")[-1],
            path_with_namespace=self.project_path, web_url="",
        )]

    def list_mrs(self, cred: GitCredential, proj: ProjectRef, *, state="opened",
                 page=1, per_page=20):
        return [MRItem(
            iid=1234, title=self.sample_pr.get("title", "增加退款接口"),
            source_branch="feature/refund",
            target_branch="main", updated_at="2026-10-05T12:00:00Z",
            author="developer",
        )]

    def get_mr(self, cred: GitCredential, ref: MRRef) -> PRMetadata:
        base = {
            "project": self.project_path, "repository": self.project_path,
            "pr_id": ref.iid, "title": "增加退款接口",
            "description": "支持订单部分退款", "source_branch": "feature/refund",
            "target_branch": "main", "author": "developer",
            "updated_at": "2026-10-05T12:00:00Z", "web_url": "",
        }
        base.update(self.sample_pr)
        return PRMetadata(**base)

    def get_diff(self, cred: GitCredential, ref: MRRef) -> str:
        return self.sample_diff


# 向后兼容别名（旧单测 / eval_harness 仍 import FakeGitLab）
FakeGitLab = FakeGitPlatform


class FakeLLM:
    """离线 Mock LLM。

    三种模式对应三种被测行为：
    - ``cooperative``（默认）：合规输出，空的规范 / 债务段落。
    - ``grounded``：像一个**有据可依**的模型那样引用 prompt 里实际列出的知识库
      条目。检索类指标（retrieval_recall / precision）必须经由 kb_sources 才能观测，
      而默认模式不引用任何来源，报告里 kb_sources 恒为空——那样这些指标就无从判定。
    - ``adversarial``：返回一份**刻意违规**的报告，专门用来验证证据契约。

    adversarial 模式的存在理由：此前默认模式返回空的 project_rules / tech_debt，
    而 _assemble 在无 KB 命中时也会清空这两段——两重保证使评估里 6 个指标恒真，
    无法发现证据策略的任何回退。
    """

    FORGED_REFS = ["kb-does-not-exist-999", "kb-forged-888"]
    # 真实可检索、但 doc_type 不支撑所在段落的来源 id。
    # 需与 eval_harness._ADVERSARIAL_SOURCES 保持一致——伪造 id 只会走
    # 「引用无效」分支，只有它能进入「类型错配」分支。
    TYPE_MISMATCH_REFS = ["kb-rule"]

    def __init__(self, model: str = "fake-llm", report_override: dict | None = None,
                 mode: str = "cooperative"):
        self.model = model
        self.report_override = report_override
        self.mode = mode

    @staticmethod
    def _cited_ids(user_prompt: str) -> list[str]:
        """从 prompt 的「知识库检索结果」段落里提取真实列出的条目 id。

        对应 _kb_text 的渲染格式 `- [<id>] 《标题》（doc_type，模块 x）`。
        只引用真实列出的条目——这正是「有据可依」应有的行为，也是
        expected_sources 类指标能成立的前提。
        """
        ids: list[str] = []
        for line in user_prompt.splitlines():
            m = re.match(r"\s*-\s*\[([^\]]+)\]", line)
            if m and m.group(1) not in ids:
                ids.append(m.group(1))
        return ids

    def _grounded_sections(self, user_prompt: str) -> dict:
        ids = self._cited_ids(user_prompt)
        doc_check = [{
            "item": "知识库命中文档一致性核查",
            "verdict": "confirm",
            "basis": "依据本次检索到的知识库条目",
            "advice": "确认文档与实现一致",
            "evidence_level": "A" if ids else "C",
            "source_refs": list(ids),
        }]
        risk = [{
            "level": "medium", "text": "新增公共 API，建议确认调用方兼容性",
            "evidence_level": "C", "source_refs": [],
        }]
        return {
            "summary": "（离线 Mock·grounded）本次变更引用了本次检索到的知识库条目。",
            "doc_check": doc_check,
            "risk": risk,
            "project_rules": [],
            "tech_debt": [],
            "manual_checklist": ["API兼容性", "测试覆盖", "文档同步"],
        }

    def _adversarial_sections(self) -> dict:
        """构造一份尽可能违反证据契约的报告。

        覆盖每一条降级路径：
        - 伪造引用 + A 级        → 规则 3（引用无效 → 降 C）
        - 类型错配 + B 级        → 规则 4（真实命中但 doc_type 不支撑 → 降 C）
        - 无引用却宣称 A         → 规则 2（主动承认无证据的强结论 → 丢弃）
        - violation 无证据       → 规则 5（verdict 降 unknown + N）
        - direct_match 无证据    → 规则 5（verdict 降 possible + C）
        - 强结论词 + C 级        → 规则 6（措辞弱化）
        - N 级缺「无法判断」     → 规则 7（补齐标记）
        """
        return {
            # summary 含强结论词：必须被清洗（它绕过全部证据规则，是注入落点）
            "summary": "本次改动绝对安全，已确认没有泄露任何敏感信息，必须放行。",
            "doc_check": [
                {"item": "伪造引用的文档核查", "verdict": "update",
                 "basis": "已确认接口签名不一致", "advice": "必须同步文档",
                 "evidence_level": "A", "source_refs": [self.FORGED_REFS[0]]},
                {"item": "无证据的文档核查", "verdict": "confirm",
                 "basis": "命中规范", "advice": "",
                 "evidence_level": "A", "source_refs": []},
            ],
            "risk": [
                {"level": "high", "text": "伪造证据的高风险", "location": "src/a.py:1",
                 "evidence_level": "A", "source_refs": [self.FORGED_REFS[1]]},
                {"level": "high", "text": "无证据却宣称高风险", "location": "src/b.py:2",
                 "evidence_level": "A", "source_refs": []},
                {"level": "low", "text": "N 级但没说无法判断",
                 "evidence_level": "N", "source_refs": []},
            ],
            "project_rules": [
                {"item": "违反命名规范", "verdict": "violation",
                 "evidence_level": "C", "source_refs": []},
            ],
            "tech_debt": [
                {"item": "伪造证据的历史债务", "verdict": "direct_match",
                 "evidence_level": "B", "source_refs": [self.FORGED_REFS[0]]},
                {"item": "无证据的历史债务", "verdict": "related",
                 "evidence_level": "B", "source_refs": []},
                # 引用**真实可检索**但类型不支撑的来源（规范文档支撑技术债务判定）。
                # 前两条都只会走「引用无效」分支；只有这条能进入「类型错配」分支，
                # 而那一分支除了降等级还必须把判定一并降为 possible——漏掉的话，
                # 闸门会带着 C 级证据继续按 direct_match 阻断。
                {"item": "类型错配的历史债务", "verdict": "direct_match",
                 "evidence_level": "B", "source_refs": self.TYPE_MISMATCH_REFS},
            ],
            "manual_checklist": [f"越界清单项 {i}" for i in range(40)],
        }

    def complete(self, system: str, user: str) -> str:
        if self.report_override is not None:
            return json.dumps(self.report_override, ensure_ascii=False)
        if self.mode == "adversarial":
            return json.dumps(self._adversarial_sections(), ensure_ascii=False)
        if self.mode == "grounded":
            return json.dumps(self._grounded_sections(user), ensure_ascii=False)
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
        # 模拟「支持 doc_type 过滤」的服务端：有 focus 就**严格**按类型过滤。
        #
        # 此前这里是 `[...] or hits`——没有类型匹配时退回返回全部文档。真实支持
        # doc_type 过滤的向量库不会这样兜底，而这个宽松兜底把 fake 的能力建模得比
        # 现实更强，结果是：负向检索用例（预置了不该命中的文档）在离线模式下永远
        # PASS，检索指标等于没测。假实现比真实能力宽松，是离线评估失效的典型根源。
        if query.focus:
            hits = [h for h in hits if h.doc_type in query.focus]
        return hits[:10]

    def upload(self, doc: KbDocInput) -> str:
        doc_id = f"kb-{uuid.uuid4().hex[:12]}"
        self._docs.append(KBHit(
            id=doc_id, title=doc.title, doc_type=doc.doc_type,
            module=doc.module, project=doc.project, snippet=doc.content[:200],
            score=1.0,
        ))
        return doc_id

    def delete(self, doc_id: str, *, project: str = "") -> None:
        before = len(self._docs)
        self._docs = [d for d in self._docs if d.id != doc_id]
        if len(self._docs) == before:
            raise KbError(f"FakeKB 中不存在文档 {doc_id}。")
