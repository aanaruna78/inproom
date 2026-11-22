from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.db import get_engine
from app.errors import problem

router = APIRouter(tags=["health"])


class Health(BaseModel):
    status: str
    version: str


class Readiness(BaseModel):
    status: str
    checks: dict[str, str]


def run_readiness_checks() -> dict[str, str]:
    """Return check name -> 'ok' or a short failure reason. Never raises."""
    checks: dict[str, str] = {}
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
            checks["database"] = "ok"
            installed = conn.execute(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            ).scalar_one_or_none()
            checks["pgvector"] = "ok" if installed else "extension not installed (run: make migrate)"
    except SQLAlchemyError as exc:
        checks["database"] = f"unreachable: {type(exc.__cause__ or exc).__name__}"
        checks["pgvector"] = "skipped: database unreachable"
    return checks


@router.get("/healthz", response_model=Health, summary="Process liveness (no dependencies)")
def healthz() -> Health:
    return Health(status="ok", version=get_settings().version)


@router.get(
    "/readyz",
    response_model=Readiness,
    summary="Dependency readiness",
    responses={503: {"description": "One or more dependencies failed (problem+json)"}},
)
def readyz(request: Request) -> Readiness | JSONResponse:
    checks = run_readiness_checks()
    failed = sorted(name for name, result in checks.items() if result != "ok")
    if failed:
        return problem(
            request,
            503,
            "Service not ready",
            "NOT_READY",
            detail=f"Failed checks: {', '.join(failed)}",
            checks=checks,
        )
    return Readiness(status="ready", checks=checks)
