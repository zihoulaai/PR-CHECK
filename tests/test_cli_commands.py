"""kb 子命令测试（CLI-only）。

验证：kb upload/list 在 Fake KB 下可往返。
"""
from __future__ import annotations

import argparse
import json

from bin.pr_check_cli import cmd_kb


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
