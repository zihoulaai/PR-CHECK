"""kb 子命令测试（CLI-only）。

验证：kb upload/list 在 Fake KB 下可往返；kb import 批量导入（全部成功 /
部分失败不拖垮整批 / --prune 标记过期来源）。
"""
from __future__ import annotations

import argparse
import json

import pytest

from app.cli import cmd_kb


def _ns(**over):
    return argparse.Namespace(**over)


def test_kb_upload_and_list(container, capsys):
    ns = _ns(kb_action="upload", file=__file__, project="team/order",
             doc_type="api_document", module="pay", title="支付接口")
    rc = cmd_kb(ns)
    assert rc == 0
    uploaded = json.loads(capsys.readouterr().out)
    assert uploaded["id"] and uploaded["project"] == "team/order"

    ns2 = _ns(kb_action="list", project="team/order", module=None, doc_type=None)
    rc2 = cmd_kb(ns2)
    assert rc2 == 0
    listed = json.loads(capsys.readouterr().out)
    assert any(d["id"] == uploaded["id"] for d in listed)


def _import_ns(directory: str, *, prune: bool = False) -> argparse.Namespace:
    return _ns(kb_action="import", dir=directory, project="team/order",
               doc_type="development_rule", module="pay", prune=prune)


def test_kb_import_batch_success(container, capsys, tmp_path):
    """递归导入目录下全部文本文件：rc=0，imported 含子目录文件，title 取 stem。"""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "rule1.md").write_text("编码规范一", encoding="utf-8")
    (docs / "rule2.md").write_text("编码规范二", encoding="utf-8")
    (docs / "sub").mkdir()
    (docs / "sub" / "rule3.md").write_text("编码规范三", encoding="utf-8")

    rc = cmd_kb(_import_ns(str(docs)))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["total"] == 3
    assert out["failed"] == []
    assert sorted(i["title"] for i in out["imported"]) == ["rule1", "rule2", "rule3"]
    # rglob 递归：子目录文件必须导入
    assert any("sub" in i["file"] for i in out["imported"])

    # metadata 落库且为 active，list 可见
    rc2 = cmd_kb(_ns(kb_action="list", project="team/order", module=None,
                     doc_type=None))
    assert rc2 == 0
    listed = json.loads(capsys.readouterr().out)
    assert len(listed) == 3
    assert all(d["status"] == "active" for d in listed)


def test_kb_import_partial_failure_returns_kb_exit(container, capsys, tmp_path):
    """单篇读取失败记入 failed 继续：rc=6，其余正常导入，隐藏文件跳过。"""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "ok.md").write_text("正常文档", encoding="utf-8")
    (docs / "bad.bin").write_bytes(b"\xff\xfe\x00\x01binary")
    (docs / ".hidden.md").write_text("隐藏文件不应导入", encoding="utf-8")

    rc = cmd_kb(_import_ns(str(docs)))
    out = json.loads(capsys.readouterr().out)
    assert rc == 6  # EXIT_KB
    assert out["total"] == 2  # 隐藏文件不计入总数
    assert len(out["imported"]) == 1
    assert len(out["failed"]) == 1
    assert out["failed"][0]["file"].endswith("bad.bin")
    assert "读取失败" in out["failed"][0]["error"]


def test_kb_import_prune_marks_stale(container, capsys, tmp_path):
    """--prune：本批未覆盖的既有 active 文档标记 stale，kb list --status stale 可见。"""
    a = tmp_path / "a"
    a.mkdir()
    (a / "old.md").write_text("旧文档", encoding="utf-8")
    assert cmd_kb(_import_ns(str(a))) == 0
    old_id = json.loads(capsys.readouterr().out)["imported"][0]["id"]

    b = tmp_path / "b"
    b.mkdir()
    (b / "new.md").write_text("新文档", encoding="utf-8")
    assert cmd_kb(_import_ns(str(b), prune=True)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["marked_stale"] == [old_id]

    assert cmd_kb(_ns(kb_action="list", project="team/order", module=None,
                      doc_type=None, status="stale")) == 0
    stale = json.loads(capsys.readouterr().out)
    assert [d["id"] for d in stale] == [old_id]

    assert cmd_kb(_ns(kb_action="list", project="team/order", module=None,
                      doc_type=None, status="active")) == 0
    active = json.loads(capsys.readouterr().out)
    assert old_id not in [d["id"] for d in active]
    assert len(active) == 1


def test_kb_import_prune_does_not_mark_current_batch(container, capsys, tmp_path):
    """重复导入同批 + --prune：keep_ids 保护本批文档不被误标 stale。"""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "rule.md").write_text("规范", encoding="utf-8")
    first = cmd_kb(_import_ns(str(docs)))
    assert first == 0
    id1 = json.loads(capsys.readouterr().out)["imported"][0]["id"]

    # 第二次导入同目录内容（新 id）+ prune：旧 id 被标过期，本批保留
    assert cmd_kb(_import_ns(str(docs), prune=True)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["marked_stale"] == [id1]
    new_id = out["imported"][0]["id"]
    assert new_id != id1

    # 第三次导入 + prune：本批文档（含上一批的新 id）不再动
    assert cmd_kb(_import_ns(str(docs), prune=True)) == 0
    out3 = json.loads(capsys.readouterr().out)
    assert out3["marked_stale"] == [new_id]
    assert out3["imported"][0]["id"] not in out3["marked_stale"]


def test_kb_import_dir_missing_raises(container, tmp_path):
    """目录不存在应抛 ValidationError（退出码 2 由 main 统一映射）。"""
    import pytest
    from app.errors import ValidationError

    with pytest.raises(ValidationError):
        cmd_kb(_import_ns(str(tmp_path / "not-exist")))


def test_kb_upload_non_utf8_friendly_error(container, tmp_path):
    """二进制 / 非 UTF-8 文件：友好 INVALID_REQUEST（rc 2），不落 INTERNAL_ERROR。"""
    from app.errors import ValidationError

    bad = tmp_path / "blob.bin"
    bad.write_bytes(b"\xff\xfe\x00\x01binary")
    ns = _ns(kb_action="upload", file=str(bad), project="team/order",
             doc_type="api_document", module="pay", title="二进制")
    with pytest.raises(ValidationError, match="UTF-8"):
        cmd_kb(ns)


def test_kb_upload_gbk_text_friendly_error(container, tmp_path):
    """GBK 等单字节编码同样走友好报错（utf-8 解码失败即可判定）。"""
    from app.errors import ValidationError

    gbk = tmp_path / "gbk.txt"
    gbk.write_bytes("中文规范".encode("gbk"))
    ns = _ns(kb_action="upload", file=str(gbk), project="team/order",
             doc_type="api_document", module="pay", title="")
    with pytest.raises(ValidationError, match="UTF-8"):
        cmd_kb(ns)


# ===== P3 kb delete =====
def _del_ns(doc_id: str) -> argparse.Namespace:
    return _ns(kb_action="delete", doc_id=doc_id)


def test_kb_delete_removes_doc_and_meta(container, capsys, tmp_path):
    """delete：向量库与本地元数据一并删除，list 不再可见。"""
    from app.domain.schemas import KBQuery

    doc = tmp_path / "rule.md"
    doc.write_text("规范", encoding="utf-8")
    ns = _ns(kb_action="upload", file=str(doc), project="team/order",
             doc_type="api_document", module="pay", title="规范")
    assert cmd_kb(ns) == 0
    doc_id = json.loads(capsys.readouterr().out)["id"]

    rc = cmd_kb(_del_ns(doc_id))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out == {"id": doc_id, "deleted": True, "local_meta_removed": True}

    assert cmd_kb(_ns(kb_action="list", project=None, module=None, doc_type=None)) == 0
    assert json.loads(capsys.readouterr().out) == []
    # FakeKB 内也确实删除（检索不再命中）
    assert all(h.id != doc_id for h in container.kb.search(KBQuery(project="team/order")))


def test_kb_delete_unknown_id_raises(container):
    """本地无此 id：INVALID_REQUEST，绝不动向量库。"""
    from app.errors import ValidationError

    with pytest.raises(ValidationError, match="本地不存在"):
        cmd_kb(_del_ns("kb-notexist"))


def test_kb_delete_provider_failure_keeps_meta(container, capsys, tmp_path, monkeypatch):
    """供应商删除失败 → KbError（rc 6），本地元数据保留以便重试。"""
    from app.errors import KbError

    doc = tmp_path / "rule.md"
    doc.write_text("规范", encoding="utf-8")
    ns = _ns(kb_action="upload", file=str(doc), project="team/order",
             doc_type="api_document", module="pay", title="规范")
    assert cmd_kb(ns) == 0
    doc_id = json.loads(capsys.readouterr().out)["id"]

    def boom(_id):
        raise KbError("供应商删除失败")

    monkeypatch.setattr(container.kb, "delete", boom)
    with pytest.raises(KbError):
        cmd_kb(_del_ns(doc_id))

    # 元数据仍在
    assert cmd_kb(_ns(kb_action="list", project=None, module=None, doc_type=None)) == 0
    assert [d["id"] for d in json.loads(capsys.readouterr().out)] == [doc_id]
