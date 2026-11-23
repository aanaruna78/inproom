"""RFC 7807 problem+json responses used by every INPROOM endpoint."""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_JSON = "application/problem+json"


def problem(
    request: Request, status: int, title: str, code: str, detail: str | None = None, **extra: Any
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"https://inproom.dev/problems/{code.lower().replace('_', '-')}",
        "title": title,
        "status": status,
        "code": code,
        "trace_id": getattr(request.state, "trace_id", None),
    }
    if detail:
        body["detail"] = detail
    body.update(extra)
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON)


def install_error_handlers(app: FastAPI) -> None:
    # Imported here to avoid import cycles (these modules use `problem`-free exceptions only).
    from app.api_errors import ApiError
    from app.auth import AuthError

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError) -> JSONResponse:
        return problem(request, exc.status, exc.title, exc.code, detail=exc.detail, **exc.extra)

    from app.authz import Forbidden, NotFound
    from app.idempotency import IdempotencyError

    @app.exception_handler(AuthError)
    async def auth_error(request: Request, exc: AuthError) -> JSONResponse:
        response = problem(request, 401, "Authentication required", exc.code, detail=exc.detail)
        response.headers["WWW-Authenticate"] = 'Bearer realm="inproom"'
        return response

    @app.exception_handler(Forbidden)
    async def forbidden(request: Request, exc: Forbidden) -> JSONResponse:
        return problem(
            request,
            403,
            "Forbidden",
            exc.code,
            detail=f"Missing permission: {exc.permission}" if exc.code == "FORBIDDEN" else exc.reason,
            required_permission=exc.permission,
        )

    @app.exception_handler(NotFound)
    async def not_found(request: Request, exc: NotFound) -> JSONResponse:
        return problem(request, 404, "Not found", "NOT_FOUND", detail=f"{exc.resource} not found")

    @app.exception_handler(IdempotencyError)
    async def idempotency_error(request: Request, exc: IdempotencyError) -> JSONResponse:
        return problem(request, exc.status, "Idempotency conflict", exc.code, detail=exc.detail)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem(request, exc.status_code, str(exc.detail), f"HTTP_{exc.status_code}")

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return problem(request, 422, "Request validation failed", "VALIDATION_ERROR", errors=exc.errors())
