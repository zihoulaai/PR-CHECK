"""按项目分知识库（KB_DATASET_MAP / RoutingKB）的测试。

覆盖两件最容易出错、且失败是**静默**的事：
1. 未命中映射的项目绝不能回落到共享知识库（回落 = 隔离形同虚设且无任何报错）；
2. KB_DATASET_MAP 写错时必须拒绝启动（静默忽略会让人「以为已经按项目隔离」）。
"""
from __future__ import annotations

import pytest

from app.adapters.base import KbDocInput
from app.adapters.routing_kb import NoDatasetConfigured, RoutingKB
from app.config import Settings, parse_dataset_map
from app.domain.schemas import KBHit, KBQuery
from app.errors import NotConfiguredError, ValidationError


class RecordingKB:
    """按项目区分的记录型适配器，用于断言「操作是否落到了正确的库」。"""

    def __init__(self, name: str):
        self.name = name
        self.calls: list[tuple] = []

    def search(self, query: KBQuery) -> list[KBHit]:
        self.calls.append(("search", query.project))
        return [KBHit(id=f"{self.name}-hit", title=self.name, doc_type="technical_debt")]

    def upload(self, doc: KbDocInput) -> str:
        self.calls.append(("upload", doc.project))
        return f"{self.name}-new"

    def delete(self, doc_id: str, *, project: str = "") -> None:
        self.calls.append(("delete", doc_id, project))

    def list_documents(self, project: str = "") -> list[dict]:
        self.calls.append(("list", project))
        return [{"id": f"{self.name}-1", "name": self.name, "word_count": 1}]


def _routing() -> tuple[RoutingKB, RecordingKB, RecordingKB]:
    a, b = RecordingKB("A"), RecordingKB("B")
    return RoutingKB({"pr-check": a, "team/order": b}, strict=True), a, b


# ===== 路由 =====
def test_search_routed_to_matching_project():
    kb, a, b = _routing()
    assert [h.id for h in kb.search(KBQuery(project="pr-check"))] == ["A-hit"]
    assert a.calls == [("search", "pr-check")]
    assert b.calls == []


def test_unmapped_project_does_not_fall_back_to_shared_dataset():
    """核心隔离保证：未命中映射 → 无库，绝不落到别人的 dataset。

    若允许回落，「忘了给某项目建库」会静默使用共享库，跨项目证据再次混入，
    而且没有任何报错——这正是本次要消除的失败形态。
    """
    kb, a, b = _routing()
    assert kb.search(KBQuery(project="team/unknown")) == []
    assert a.calls == [] and b.calls == [], "未命中时不得触碰任何已绑定的库"
    assert not kb.has_dataset("team/unknown")


def test_upload_to_unmapped_project_raises_not_typed_noqa():
    kb, _, _ = _routing()
    with pytest.raises(NotConfiguredError) as exc:
        kb.upload(KbDocInput(project="team/unknown", module="m", doc_type="d",
                             title="t", content="c"))
    # 报错必须点名是哪个项目、已配置了哪些，否则用户无从下手
    assert "team/unknown" in str(exc.value)
    assert "pr-check" in str(exc.value)


def test_empty_project_never_routed_to_any_dataset():
    kb, a, b = _routing()
    assert kb.search(KBQuery(project="")) == []
    assert a.calls == [] and b.calls == []


def test_upload_and_delete_route_correctly():
    kb, a, b = _routing()
    assert kb.upload(KbDocInput(project="team/order", module="m", doc_type="d",
                                title="t", content="c")) == "B-new"
    kb.delete("doc-1", project="pr-check")
    assert a.calls == [("upload" if False else "delete", "doc-1", "pr-check")] or \
        a.calls[-1] == ("delete", "doc-1", "pr-check")
    assert b.calls[0] == ("upload", "team/order")


def test_delete_without_project_is_rejected_not_guessed():
    """不猜：多个库里删错项目的数据不可逆。"""
    kb, a, _ = _routing()
    with pytest.raises(ValidationError) as exc:
        kb.delete("doc-1")
    assert "--project" in str(exc.value)
    assert a.calls == [], "拒绝时不得触碰任何库"


def test_list_documents_requires_project_and_forwards_it():
    kb, a, _ = _routing()
    with pytest.raises(ValidationError):
        kb.list_documents()
    assert [d["name"] for d in kb.list_documents("pr-check")] == ["A"]
    assert a.calls == [("list", "pr-check")]


def test_projects_property_sorted():
    kb, _, _ = _routing()
    assert kb.projects == ["pr-check", "team/order"]
    assert kb.strict is True


# ===== NoDatasetConfigured =====
def test_no_dataset_search_is_empty_but_write_paths_raise():
    nd = NoDatasetConfigured("nope", ["pr-check"])
    assert nd.search(KBQuery(project="nope")) == []
    for call in (lambda: nd.upload(KbDocInput(project="nope", module="", doc_type="",
                                              title="", content="")),
                 lambda: nd.delete("x"),
                 lambda: nd.list_documents("nope")):
        with pytest.raises(NotConfiguredError):
            call()


# ===== 配置解析 =====
@pytest.mark.parametrize("raw,expected", [
    ('{"pr-check":"uuid-1"}', {"pr-check": "uuid-1"}),
    ('{"team/order":"uuid-2"}', {"team/order": "uuid-2"}),
    ('  {"a" : "b" }  ', {"a": "b"}),
    ("", {}),
    ("   ", {}),
])
def test_parse_dataset_map_valid(raw, expected):
    assert parse_dataset_map(raw) == expected


@pytest.mark.parametrize("raw", [
    "{bad",                                  # 非法 JSON
    '["a"]',                                 # 顶层非对象
    '{"":"uuid"}',                           # 空项目名
    '{"p":""}',                              # 空 dataset id
    '{"p":123}',                             # 值非字符串
    '{"p":null}',                            # 值为 null
])
def test_parse_dataset_map_rejects_bad_input(raw):
    with pytest.raises(ValueError):
        parse_dataset_map(raw)


def _settings(**over) -> Settings:
    base = dict(llm_base_url="http://l", llm_model="m", llm_api_key="k",
                kb_base_url="http://kb", kb_api_key="ka", kb_provider="dify")
    base.update(over)
    return Settings(**base)


def test_dataset_map_requires_kb_credentials():
    with pytest.raises(ValueError) as exc:
        _settings(kb_dataset_map='{"a":"b"}', kb_api_key="")
    assert "KB_API_KEY" in str(exc.value)


def test_invalid_dataset_map_fails_fast():
    """写错映射必须拒绝启动——静默忽略会让人以为已按项目隔离。"""
    with pytest.raises(ValueError) as exc:
        _settings(kb_dataset_map="{oops")
    assert "KB_DATASET_MAP" in str(exc.value)


def test_valid_dataset_map_is_accepted():
    s = _settings(kb_dataset_map='{"pr-check":"uuid-1"}', kb_index="legacy")
    from app.config import dataset_mapping

    assert dataset_mapping(s) == {"pr-check": "uuid-1"}


def test_dataset_map_absent_keeps_legacy_single_dataset():
    s = _settings(kb_index="legacy-uuid")
    from app.config import dataset_mapping

    assert dataset_mapping(s) == {}


# ===== build_kb 集成 =====
def test_build_kb_returns_routing_when_map_configured():
    from app.adapters.registry import build_kb

    kb = build_kb(_settings(kb_dataset_map='{"pr-check":"uuid-1","team/order":"uuid-2"}'))
    assert isinstance(kb, RoutingKB)
    assert kb.projects == ["pr-check", "team/order"]
    assert kb.strict is True


def test_build_kb_returns_single_adapter_without_map(container):
    """向后兼容：未配置映射时返回单库适配器（不是 RoutingKB）。"""
    from app.adapters.dify_kb import DifyKBAdapter
    from app.adapters.registry import build_kb

    kb = build_kb(_settings(kb_index="legacy-uuid"))
    assert isinstance(kb, DifyKBAdapter)
    assert kb.dataset_id == "legacy-uuid"


def test_build_kb_per_project_adapters_get_their_own_index():
    from app.adapters.registry import build_kb

    kb = build_kb(_settings(kb_dataset_map='{"a":"uuid-a","b":"uuid-b"}'))
    assert kb.adapter_for("a").dataset_id == "uuid-a"
    assert kb.adapter_for("b").dataset_id == "uuid-b"


def test_build_kb_none_without_credentials():
    from app.adapters.registry import build_kb

    # _env_file=None：否则会读到仓库根 .env（conftest 只覆盖 DATABASE_URL）
    assert build_kb(Settings(_env_file=None)) is None


# ===== kb_status 区分 =====
def test_no_dataset_status_is_distinct_from_not_configured(container):
    """「配了多库但这个项目没建」必须与「压根没配 KB」区分开。"""
    from app.agent.workflow import run_check_from_diff
    from app.container import get_container

    c = get_container()
    c.kb = RoutingKB({"pr-check": RecordingKB("A")}, strict=True)
    report = run_check_from_diff("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
                                 "@@ -1 +1,2 @@\n+x = 1\n",
                                 project="team/unknown")
    assert report.meta.kb_status.value == "no_dataset"
    # 该状态下项目特定结论必须为空（无知识不强判）
    assert report.project_rules == [] and report.tech_debt == []


def test_no_dataset_status_rendered_distinctly(container):
    from app.agent.workflow import run_check_from_diff
    from app.container import get_container
    from app.report.markdown import render_markdown

    c = get_container()
    c.kb = RoutingKB({"pr-check": RecordingKB("A")}, strict=True)
    report = run_check_from_diff("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
                                 "@@ -1 +1,2 @@\n+x = 1\n",
                                 project="team/unknown")
    md = render_markdown(report)
    assert "no_dataset" in md
    assert "未绑定知识库" in md
    assert "闸门" in md, "必须提示闸门不会触发——这是运维最容易踩空的地方"


def test_unmapped_project_search_touches_no_dataset(container):
    """端到端：未绑定项目的检索不得触碰任何已绑定库。"""
    from app.agent.workflow import run_check_from_diff
    from app.container import get_container

    bound = RecordingKB("A")
    c = get_container()
    c.kb = RoutingKB({"pr-check": bound}, strict=True)
    run_check_from_diff("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
                        "@@ -1 +1,2 @@\n+x = 1\n", project="team/unknown")
    assert bound.calls == [], f"未绑定项目却触碰了已绑定库：{bound.calls}"
