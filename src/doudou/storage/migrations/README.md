# storage/migrations/

**当前状态：暂缓。** M2 用 `Base.metadata.create_all` 建表（见 `storage/database.py`）。

理由：schema 还是全新的、没有任何线上数据要迁移，此刻引入 Alembic 只有仪式感。
**触发条件**：第一次需要修改既有表结构时（加字段、改类型、加索引），
必须先补上 Alembic，而不是手写 `ALTER TABLE`。

## 届时的落地方式

```
migrations/
├─ env.py            # 绑定 storage/models.py 的 Base.metadata
├─ script.py.mako
├─ alembic.ini       # 或配置在 pyproject.toml 的 [tool.alembic]
└─ versions/         # 迁移脚本（自动生成，已排除在 ruff 之外）
```

异步引擎需要 `env.py` 用 `connection.run_sync(context.run_migrations)`；
可参考 SQLAlchemy 官方的 async 模板。

```powershell
uv run alembic revision --autogenerate -m "init"
uv run alembic upgrade head
uv run alembic downgrade -1
```

## 约定

- 每个迁移必须可回滚（写 `downgrade()`）。
- 模型变更后必须附带迁移脚本，禁止只改 `storage/models.py`。
- 生产环境在应用启动**之前**独立执行迁移，不要放进 lifespan。
- 启用 Alembic 后，把 `Database.create_schema()` 从 lifespan 里摘掉，
  改为仅在建库/测试场景使用。
