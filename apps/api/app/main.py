import re
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response

from app.config import get_settings
from app.errors import install_error_handlers
from app.health import router as health_router
from app.me import router as me_router
from app.modules.code import router as code_router
from app.modules.confluence_pages import router as confluence_pages_router
from app.modules.confluence_sources import router as confluence_sources_router
from app.modules.connection_health import router as connection_health_router
from app.modules.connections import router as connections_router
from app.modules.content import router as content_router
from app.modules.documents import router as documents_router
from app.modules.jira_issues import router as jira_issues_router
from app.modules.jira_sources import router as jira_sources_router
from app.modules.members import router as members_router
from app.modules.migration import router as migration_router
from app.modules.projects import router as projects_router
from app.modules.provenance import router as provenance_router
from app.modules.settings import router as settings_router
from app.modules.sources import router as sources_router
from app.modules.sync_status import router as sync_status_router
from app.modules.webhooks import router as webhooks_router
from app.modules.workspaces import router as workspaces_router

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
    app.include_router(me_router)
    app.include_router(workspaces_router)
    app.include_router(projects_router)
    app.include_router(members_router)
    app.include_router(settings_router)
    # before connections: /v1/connections/attention must not be read as /v1/connections/{connection_id}
    app.include_router(connection_health_router)
    app.include_router(connections_router)
    app.include_router(sources_router)
    app.include_router(jira_sources_router)
    app.include_router(jira_issues_router)
    app.include_router(confluence_sources_router)
    app.include_router(confluence_pages_router)
    app.include_router(sync_status_router)
    app.include_router(migration_router)
    app.include_router(provenance_router)  # before content: its item route catches every longer path
    app.include_router(content_router)
    app.include_router(documents_router)
    app.include_router(code_router)
    app.include_router(webhooks_router)
    return app


app = create_app()
