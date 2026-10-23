.DEFAULT_GOAL := help
PY := uv run
PKG := src/doudou

.PHONY: help
help: ## 显示所有可用目标
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- 安装
.PHONY: install
install: ## 安装全部依赖（含各 provider 与 dev）
	uv sync --all-extras

.PHONY: lock
lock: ## 刷新 uv.lock
	uv lock --upgrade

# ---------------------------------------------------------------- 运行
.PHONY: serve
serve: ## 启动 FastAPI（开发模式，热重载）
	$(PY) doudou serve --reload

.PHONY: chat
chat: ## 启动终端交互式对话
	$(PY) doudou chat

.PHONY: serve-prod
serve-prod: ## 以生产参数启动 API
	$(PY) uvicorn doudou.api.app:app --host 0.0.0.0 --port 8000 --workers 4

# ---------------------------------------------------------------- 质量
.PHONY: fmt
fmt: ## 格式化代码
	$(PY) ruff format .
	$(PY) ruff check --fix .

.PHONY: lint
lint: ## 静态检查（ruff + mypy）
	$(PY) ruff check .
	$(PY) ruff format --check .
	$(PY) mypy $(PKG)

.PHONY: test
test: ## 运行默认测试套件（离线，不含 integration 标记）
	$(PY) pytest

.PHONY: test-all
test-all: ## 运行全部测试（含需要外部依赖的）
	$(PY) pytest -m ""

.PHONY: cov
cov: ## 测试并输出覆盖率
	$(PY) pytest --cov=$(PKG) --cov-report=term-missing --cov-report=html

.PHONY: pre-commit
pre-commit: ## 安装 git hooks
	$(PY) pre-commit install

# ---------------------------------------------------------------- 辅助
.PHONY: skills
skills: ## 列出已发现的 Skill
	$(PY) doudou skills list

.PHONY: config
config: ## 打印生效配置（脱敏）
	$(PY) doudou config show

.PHONY: check
check: ## 校验配置、凭据、工具与 Skill
	$(PY) doudou config check

.PHONY: clean
clean: ## 清理缓存与构建产物
	@rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage dist build
	@find . -type d -name __pycache__ -prune -exec rm -rf {} +

# ---------------------------------------------------------------- Docker
.PHONY: docker-build
docker-build: ## 构建镜像
	docker build -t doudou:local .

.PHONY: docker-up
docker-up: ## docker compose 启动
	docker compose up -d --build

.PHONY: docker-down
docker-down: ## docker compose 停止
	docker compose down
