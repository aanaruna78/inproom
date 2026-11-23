"""Domain errors rendered as RFC 7807 problem+json by app.errors."""


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        title: str,
        detail: str | None = None,
        headers: dict[str, str] | None = None,
        **extra,
    ):
        super().__init__(detail or title)
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail
        self.headers = headers or {}  # e.g. Retry-After
        self.extra = extra  # additional problem+json members, e.g. field errors


def conflict(code: str, detail: str) -> ApiError:
    return ApiError(409, code, "Conflict", detail)


def unprocessable(code: str, detail: str) -> ApiError:
    return ApiError(422, code, "Unprocessable request", detail)


def precondition_required(detail: str) -> ApiError:
    return ApiError(428, "PRECONDITION_REQUIRED", "Precondition required", detail)


def precondition_failed(detail: str) -> ApiError:
    return ApiError(412, "PRECONDITION_FAILED", "Precondition failed", detail)
