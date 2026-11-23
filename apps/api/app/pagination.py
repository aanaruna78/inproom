"""Opaque keyset cursors over (created_at, id)."""

import base64
import json
import uuid
from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel
from sqlalchemy import Column, Select, and_, or_

from app.api_errors import ApiError

T = TypeVar("T")
MAX_LIMIT = 100


class Page(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = None


def encode_cursor(created_at: datetime, row_id: uuid.UUID) -> str:
    raw = json.dumps([created_at.isoformat(), str(row_id)]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        created_at, row_id = json.loads(raw)
        return datetime.fromisoformat(created_at), uuid.UUID(row_id)
    except (ValueError, TypeError) as exc:
        raise ApiError(400, "INVALID_CURSOR", "Invalid cursor", "cursor is malformed") from exc


def apply_keyset(stmt: Select, created_col: Column, id_col: Column, cursor: str | None, limit: int) -> Select:
    if cursor:
        created_at, row_id = decode_cursor(cursor)
        stmt = stmt.where(or_(created_col > created_at, and_(created_col == created_at, id_col > row_id)))
    return stmt.order_by(created_col, id_col).limit(limit + 1)


def next_cursor(rows: list, limit: int) -> tuple[list, str | None]:
    """Trim the look-ahead row and build the cursor for the next page."""
    if len(rows) <= limit:
        return rows, None
    page = rows[:limit]
    last = page[-1]
    return page, encode_cursor(last["created_at"], last["id"])
