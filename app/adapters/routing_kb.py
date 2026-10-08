"""按项目路由的知识库适配器（多知识库隔离）。

解决的问题：一个知识库（dataset）里混放多个项目的文档时，检索会把别的项目的规范
一起召回，并被当作本项目的 A/B 级证据写进报告。Dify 的 ``metadata_filtering``
不可依赖——实测它接受参数但静默忽略（必然不匹配的条件仍照常返回记录），而文档
也不支持携带 project 元数据（``documents/{id}`` 响应无 metadata 字段，
``documents/{id}/metadata`` 端点 404）。因此唯一可靠的隔离手段是**物理分库**。

路由规则（关键，改动前请三思）：

- 未配置 ``KB_DATASET_MAP``：所有项目走同一个适配器（``kb_index``），既有
  单知识库用户的行为完全不变。
- 已配置 ``KB_DATASET_MAP``：进入**严格路由**。项目命中映射才走对应知识库；
  **未命中即「该项目没有知识库」**，绝不回落到 ``kb_index``。若允许回落，
  「忘了给某项目建库」就会静默落进共享库，把刚拆开的隔离重新打开且毫无提示。

本类按结构化类型实现 ``KnowledgeBase`` 协议（与既有适配器一致），因此
workflow / container / 评估脚本无需感知多库这件事。
"""
from __future__ import annotations

from app.adapters.base import KnowledgeBase, KbDocInput
from app.domain.schemas import KBHit, KBQuery
from app.errors import NotConfiguredError, ValidationError


class NoDatasetConfigured:
    """该项目没有绑定任何知识库（严格路由下未命中 KB_DATASET_MAP）。

    刻意做成一个「如实记录」的适配器，而不是让每个调用方到处判空：

    - ``search`` 返回空 —— 报告因此没有知识段落、kb_status=no_dataset；
    - ``upload`` / ``delete`` / ``list_documents`` 显式报错 —— 写入路径绝不能
      静默成功，那会造成「以为已经传进去了」的信任陷阱。
    """

    def __init__(self, project: str, configured: list[str] | None = None):
        self.project = project
        self.configured = list(configured or [])

    def _why(self) -> str:
        if self.configured:
            return (f"项目 {self.project!r} 未配置知识库：KB_DATASET_MAP 里只有 "
                    f"{', '.join(self.configured)}。请为该项目建库并加入映射。")
        return (f"项目 {self.project!r} 未配置知识库，且未设置 KB_DATASET_MAP。"
                f"请配置 KB_INDEX（单库）或 KB_DATASET_MAP（按项目分库）。")

    def search(self, query: KBQuery) -> list[KBHit]:
        return []

    def upload(self, doc: KbDocInput) -> str:
        raise NotConfiguredError(self._why())

    def delete(self, doc_id: str, *, project: str = "") -> None:
        raise NotConfiguredError(self._why())

    def list_documents(self, project: str = "") -> list[dict]:
        raise NotConfiguredError(self._why())


class RoutingKB:
    """按 project 把 KB 操作路由到对应的单库适配器。"""

    def __init__(self, per_project: dict[str, KnowledgeBase],
                 *, strict: bool = True):
        """strict=True（配置了 KB_DATASET_MAP 时）：未命中即无库，不回落。

        strict=False：未命中时无库（同样不回落到共享库）——保留参数是为了把
        「是否严格」显式写在构造处，而不是散落在调用方。
        """
        self._per_project = dict(per_project)
        self._strict = strict

    @property
    def strict(self) -> bool:
        return self._strict

    @property
    def projects(self) -> list[str]:
        return sorted(self._per_project)

    def has_dataset(self, project: str) -> bool:
        """该项目是否绑定了知识库。供上层区分 no_dataset 与 not_configured。"""
        return project in self._per_project

    def adapter_for(self, project: str) -> KnowledgeBase:
        kb = self._per_project.get(project)
        if kb is None:
            # 传项目名列表而非 dict：_why() 要把它 join 成「已配置哪些项目」的提示
            return NoDatasetConfigured(project, self.projects)
        return kb

    # ===== KnowledgeBase 协议 =====
    def search(self, query: KBQuery) -> list[KBHit]:
        # 空 project 走「无库」分支：KBQuery.project 必填、禁止跨项目检索，
        # 这里不猜、也不回落到默认库。
        return self.adapter_for(query.project).search(query)

    def upload(self, doc: KbDocInput) -> str:
        return self.adapter_for(doc.project).upload(doc)

    def delete(self, doc_id: str, *, project: str = "") -> None:
        """按 project 删除。

        ``project`` 为空时（受保护：CLI 会先用本地 KbDoc 元数据反查补齐），此处
        显式报错而不是猜——多个库里出现同 id 的概率虽低，但猜错就是删掉别的项目的
        数据，且不可逆。
        """
        if not project:
            raise ValidationError(
                "按项目分库时删除必须指明所属项目；CLI 会先用本地元数据反查，"
                "查不到时请显式加 --project。")
        self.adapter_for(project).delete(doc_id, project=project)

    # ===== 可选能力（供应商未必实现）=====
    def list_documents(self, project: str = "") -> list[dict]:
        if not project:
            raise ValidationError("按项目分库时列举文档必须指明项目（--project）。")
        lister = getattr(self.adapter_for(project), "list_documents", None)
        if not callable(lister):
            raise NotImplementedError(
                f"项目 {project!r} 的知识库适配器不支持列举文档"
                f"（list_documents 未实现）。")
        # 必须把 project 转发下去：此前写成 lister() 会静默丢掉它，
        # 适配器收到空 project 却不报错——正是本项目最该消除的那类静默失败。
        return lister(project=project)

