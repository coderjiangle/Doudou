"""FastAPI application factory."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from doudou.api.errors import install_error_handlers
from doudou.api.lifespan import make_lifespan
from doudou.api.middleware import AccessLogMiddleware, RequestIdMiddleware
from doudou.api.routes import build_router
from doudou.config.loader import Settings, get_settings
from doudou.observability.logging import get_logger
from doudou.version import __version__

logger = get_logger(__name__)

DESCRIPTION = """\
Doudou 个人助手 Agent 的 HTTP 接口。

* `POST /chat` —— 单轮对话，一次返回完整答复
* `POST /chat/stream` —— SSE 流式对话，逐块推送
* `GET /threads` / `GET /threads/{id}` —— 会话与历史
* `GET /tools` —— 已装载的工具
* `GET /health` —— 健康检查（不需要鉴权）

鉴权：配置了 `DOUDOU_API_TOKEN` 时，除健康检查外的接口都要求
`Authorization: Bearer <token>`。
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    """构造应用。

    Args:
        settings: 注入配置（测试用）；缺省时在启动阶段读取进程级配置。

    Returns:
        已挂载路由与中间件的 FastAPI 实例。**这里不构造模型**——
        模型与数据库连接都在 lifespan 里初始化，保证导入模块本身是廉价的。
    """
    resolved = settings or get_settings()

    app = FastAPI(
        title="Doudou",
        description=DESCRIPTION,
        version=__version__,
        lifespan=make_lifespan(settings),
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )

    # 中间件是「后加的先执行」：RequestId 要在最外层，这样 AccessLog 的日志
    # 才带得上 request_id。
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(RequestIdMiddleware)

    if resolved.app.server.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=resolved.app.server.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["X-Request-ID"],
        )

    install_error_handlers(app)

    router: APIRouter = build_router(resolved.app.server.base_path)
    app.include_router(router)

    logger.debug(
        "api.app_created",
        base_path=resolved.app.server.base_path,
        auth_required=bool(resolved.api_token),
    )
    return app


#: uvicorn / gunicorn 的入口：``uvicorn doudou.api.app:app``。
app = create_app()


__all__ = ["app", "create_app"]
