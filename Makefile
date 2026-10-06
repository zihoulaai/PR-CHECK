# PR_CHECK — 纯 CLI 工具 Makefile
#
# 运行环境：类 Unix shell（Git Bash / WSL / Linux）。
# Windows 原生 cmd 不支持 make，请用 Git Bash，或直接复制下方的 `python` 命令执行。

PYTHON   ?= python
PIP       = $(PYTHON) -m pip
CLI       = $(PYTHON) -m app.cli

.DEFAULT_GOAL := help

.PHONY: help install install-dev test version \
        check kb-upload kb-list eval \
        package clean

help: ## 显示本帮助
	@echo "可用目标（Windows 用户请用 Git Bash 运行）："
	@echo "  install          安装运行依赖"
	@echo "  install-dev      安装运行 + 开发（测试）依赖"
	@echo "  test             运行全部单测"
	@echo "  version          显示版本信息"
	@echo "  check            离线跑一段 diff（--fake）：make check DIFF=pr.diff"
	@echo "  kb-upload        上传知识文档：make kb-upload FILE=api.md PROJECT=team/order DOC_TYPE=api_document [MODULE=pay]"
	@echo "  kb-list          列出知识文档：make kb-list [PROJECT=team/order]"
	@echo "  eval             运行离线评估指标"
	@echo "  package          构建 sdist + wheel 到 dist/（需 build 包）"
	@echo "  clean            清理 __pycache__ 与 .pyc"

install: ## 安装运行依赖
	$(PIP) install -r requirements.txt

install-dev: ## 安装运行 + 开发（测试）依赖
	$(PIP) install -r requirements.txt "pytest>=8.0" "pytest-asyncio>=0.23"

test: ## 运行全部单测
	$(PYTHON) -m pytest -q

version: ## 显示版本信息
	$(CLI) version

check: ## 离线跑一段 diff（--fake）：make check DIFF=pr.diff
	$(CLI) check --diff $(or $(DIFF),-) --fake

kb-upload: ## 上传知识文档：make kb-upload FILE=api.md PROJECT=team/order DOC_TYPE=api_document [MODULE=pay] [TITLE=...]
	$(CLI) kb upload --file $(FILE) --project $(PROJECT) --doc-type $(DOC_TYPE) $(if $(MODULE),--module $(MODULE),) $(if $(TITLE),--title $(TITLE),)

kb-list: ## 列出知识文档：make kb-list [PROJECT=team/order]
	$(CLI) kb list $(if $(PROJECT),--project $(PROJECT),)

eval: ## 运行离线评估指标
	$(PYTHON) tests/eval_harness.py

package: ## 构建 sdist + wheel 到 dist/（需可联网安装 build）
	$(PIP) install --quiet --disable-pip-version-check build
	$(PYTHON) -m build

clean: ## 清理 __pycache__ / 构建产物 / dist
	$(PYTHON) -c "import pathlib, shutil; [shutil.rmtree(p, ignore_errors=True) for p in pathlib.Path('.').rglob('__pycache__')]; [shutil.rmtree(p, ignore_errors=True) for p in pathlib.Path('.').glob('dist')]; [shutil.rmtree(p, ignore_errors=True) for p in pathlib.Path('.').glob('build')]; [shutil.rmtree(p, ignore_errors=True) for p in pathlib.Path('.').rglob('*.egg-info')]"
