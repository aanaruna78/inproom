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
    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem(request, exc.status_code, str(exc.detail), f"HTTP_{exc.status_code}")

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return problem(request, 422, "Request validation failed", "VALIDATION_ERROR", errors=exc.errors())
