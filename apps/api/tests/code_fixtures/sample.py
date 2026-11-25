"""Module docstring with def fake(): inside."""
import os
from typing import TYPE_CHECKING, Any
from .models import User as U, Order

if TYPE_CHECKING:
    from app.db import Session

MAX_USERS = 100
timeout = 5


@dataclass
class Service(Base):
    """A service."""

    name: str = "svc"

    def __init__(self, repo):
        self.repo = repo

    async def fetch(self, user_id: int) -> User:
        def helper(x):
            return x

        return await self.repo.get(user_id)

    class Config:
        strict = True


def handler(event, *args, **kwargs) -> dict[str, Any]:
    return {}
