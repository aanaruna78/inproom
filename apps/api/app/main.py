import re
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response

from app.config import get_settings
from app.errors import install_error_handlers
from app.health import router as health_router

TRACE_HEADER = "X-Trace-Id"
_SAFE_TRACE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="INPROOM API",
        version=settings.version,
        description="Intelligent Process Orchestration & Management - control-plane API.",
    )

    @app.middleware("http")
    async def trace_id(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        incoming = request.headers.get(TRACE_HEADER, "")
        request.state.trace_id = incoming if _SAFE_TRACE.match(incoming) else uuid.uuid4().hex
        response = await call_next(request)
        response.headers[TRACE_HEADER] = request.state.trace_id
        return response

    install_error_handlers(app)
    app.include_router(health_router)
    return app


app = create_app()
