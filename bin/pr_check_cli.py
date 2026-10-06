#!/usr/bin/env python3
"""PR_CHECK CLI 源码入口（兼容 shim）。

CLI 主体已迁到包内 ``app.cli``（翻新的分发入口）。本文件仅为保留旧路径调用习惯：

    python bin/pr_check_cli.py <args>      # 等价于 python -m app.cli <args>

新用法推荐：``uv tool install .`` 后直接用 ``pr-check <args>``；未装机时可用 ``python -m app.cli <args>``。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.cli import EXIT_GATE, EXIT_OK, build_parser, cmd_check, cmd_hook, cmd_kb, cmd_version, main  # noqa: E402,F401

if __name__ == "__main__":
    sys.exit(main())
