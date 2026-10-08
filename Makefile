# PR_CHECK — 纯 CLI 工具 Makefile
#
# 运行环境：类 Unix shell（Git Bash / WSL / Linux）。
# Windows 原生 cmd 不支持 make，请用 Git Bash，或直接复制下方的 `python` 命令执行。

PYTHON   ?= python
PIP       = $(PYTHON) -m pip
CLI       = $(PYTHON) -m app.cli

.DEFAULT_GOAL := help

.PHONY: help install install-dev test version \
        check kb-upload kb-list eval eval-adv eval-real \
        lint typecheck typecheck-full mutation-check ci package clean

help: ## 显示本帮助
	@echo "可用目标（Windows 用户请用 Git Bash 运行）："
	@echo "  install          安装运行依赖"
	@echo "  install-dev      安装运行 + 开发（测试）依赖"
	@echo "  test             运行全部单测"
	@echo "  version          显示版本信息"
	@echo "  check            离线跑一段 diff（--fake）：make check DIFF=pr.diff"
	@echo "  kb-upload        上传知识文档：make kb-upload FILE=api.md PROJECT=team/order DOC_TYPE=api_document [MODULE=pay]"
	@echo "  kb-list          列出知识文档：make kb-list [PROJECT=team/order]"
	@echo "  eval             运行离线评估指标（Fake 模式）"
	@echo "  eval-real        真实 LLM 回归集（需配 LLM_* 环境变量）"
	@echo "  package          构建 sdist + wheel 到 dist/（需 build 包）"
	@echo "  clean            清理 __pycache__ 与 .pyc"

install: ## 安装运行依赖
	$(PIP) install .

install-dev: ## 安装运行 + 开发（测试）依赖
	$(PIP) install -e ".[dev]"

test: ## 运行全部单测
	$(PYTHON) -m pytest -q

lint: ## ruff 静态检查（配置见 pyproject [tool.ruff]）
	$(PYTHON) -m ruff check app tests

# mypy 卡门目录：实测 0 error 的部分先进门。
# 变更范围时同步更新 pyproject.toml 里 [tool.mypy] 上方的实测数字说明。
typecheck: ## mypy 类型检查（仅卡门目录）
	$(PYTHON) -m mypy app/domain app/report app/parser app/container.py app/cli.py

typecheck-full: ## mypy 全量（信息用，不设卡门：agent/adapters/storage 尚有已知 error）
	-$(PYTHON) -m mypy app

version: ## 显示版本信息
	$(CLI) version

check: ## 离线跑一段 diff（--fake）：make check DIFF=pr.diff
	$(CLI) check --diff $(or $(DIFF),-) --fake

kb-upload: ## 上传知识文档：make kb-upload FILE=api.md PROJECT=team/order DOC_TYPE=api_document [MODULE=pay] [TITLE=...]
	$(CLI) kb upload --file $(FILE) --project $(PROJECT) --doc-type $(DOC_TYPE) $(if $(MODULE),--module $(MODULE),) $(if $(TITLE),--title $(TITLE),)

kb-list: ## 列出知识文档：make kb-list [PROJECT=team/order]
	$(CLI) kb list $(if $(PROJECT),--project $(PROJECT),)

eval: ## 运行离线评估指标（Fake 模式，可复现；加 STRICT=1 则失败即报错）
	$(PYTHON) tests/eval_harness.py $(if $(STRICT),--strict,)

eval-adv: ## 对抗模式评估：用刻意违规的 LLM 输出验证证据清洗真的生效
	$(PYTHON) tests/eval_harness.py --adversarial --strict

mutation-check: ## 变异检测：注入已知缺陷，验证测试套件能否抓到（有缺陷逃过则失败退出）
	$(PYTHON) tests/mutation_check.py

ci: test eval eval-adv lint typecheck ## 本地完整门禁 = GitHub Actions 的同一入口

eval-real: ## 真实 LLM 回归集（需 .env 配齐 LLM_BASE_URL/LLM_MODEL/LLM_API_KEY）
	$(PYTHON) tests/eval_harness.py --real --strict

package: ## 构建 sdist + wheel 到 dist/（需可联网安装 build）
	$(PIP) install --quiet --disable-pip-version-check build
	$(PYTHON) -m build

clean: ## 清理 __pycache__ / 构建产物 / dist
	$(PYTHON) -c "import pathlib, shutil; [shutil.rmtree(p, ignore_errors=True) for p in pathlib.Path('.').rglob('__pycache__')]; [shutil.rmtree(p, ignore_errors=True) for p in pathlib.Path('.').glob('dist')]; [shutil.rmtree(p, ignore_errors=True) for p in pathlib.Path('.').glob('build')]; [shutil.rmtree(p, ignore_errors=True) for p in pathlib.Path('.').rglob('*.egg-info')]"
