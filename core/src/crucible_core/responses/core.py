from __future__ import annotations

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from crucible_core.logging import get_logger
from crucible_core.schemas.envelope import ErrorData, ErrorEnvelope

logger = get_logger(__name__)

ERROR_MESSAGES = {
    "EVENT_NOT_FOUND": "Event not found.",
    "TASK_NOT_FOUND": "Task not found.",
    "INVALID_CURSOR": "Cursor is invalid.",
    "IDEMPOTENCY_CONFLICT": "Event conflicts with a previous delivery.",
    "ADMISSION_CONFLICT": "Admission conflicts with existing state.",
    "STEER_WITHOUT_ACTIVE_TASK": (
        "No active task in this session to steer. Start a new request first."
    ),
}

VALIDATION_MESSAGE = "Request validation failed."
NOT_FOUND_MESSAGE = "API route not found."
METHOD_NOT_ALLOWED_MESSAGE = "API method not allowed."
UNEXPECTED_MESSAGE = "Unexpected server error."
DEFAULT_ERROR_MESSAGE = "Request failed."


class ProblemError(Exception):
    def __init__(self, code: str, status_code: int) -> None:
        self.code = code
        self.status_code = status_code


def _error_envelope(message: str, code: str) -> dict[str, object]:
    return ErrorEnvelope(
        status="error", message=message, data=ErrorData(code=code)
    ).model_dump()


def _is_api(request: Request) -> bool:
    path = request.url.path
    return path == "/v1" or path.startswith("/v1/")


async def problem_error(
    request: Request, exception: ProblemError
) -> JSONResponse:
    logger.warning(
        "api request failed code=%s status=%s path=%s",
        exception.code,
        exception.status_code,
        request.url.path,
    )
    message = ERROR_MESSAGES.get(exception.code, DEFAULT_ERROR_MESSAGE)
    return JSONResponse(
        status_code=exception.status_code,
        content=_error_envelope(message, exception.code),
    )


async def api_error(
    request: Request, exception: StarletteHTTPException
) -> JSONResponse:
    logger.warning(
        "api http error status=%s path=%s",
        exception.status_code,
        request.url.path,
    )
    if _is_api(request):
        if exception.status_code == 404:
            return JSONResponse(
                status_code=exception.status_code,
                content=_error_envelope(
                    NOT_FOUND_MESSAGE, "API_ROUTE_NOT_FOUND"
                ),
                headers=exception.headers,
            )
        if exception.status_code == 405:
            return JSONResponse(
                status_code=exception.status_code,
                content=_error_envelope(
                    METHOD_NOT_ALLOWED_MESSAGE, "API_METHOD_NOT_ALLOWED"
                ),
                headers=exception.headers,
            )
        return JSONResponse(
            status_code=exception.status_code,
            content=_error_envelope(
                DEFAULT_ERROR_MESSAGE, "API_REQUEST_FAILED"
            ),
            headers=exception.headers,
        )

    return JSONResponse(
        status_code=exception.status_code,
        content={"detail": exception.detail},
        headers=exception.headers,
    )


async def validation_error(
    request: Request, exception: RequestValidationError
) -> JSONResponse:
    logger.warning("request validation failed path=%s", request.url.path)
    if not _is_api(request):
        return JSONResponse(
            status_code=422, content={"detail": exception.errors()}
        )
    return JSONResponse(
        status_code=422,
        content=_error_envelope(
            VALIDATION_MESSAGE, "REQUEST_VALIDATION_FAILED"
        ),
    )


async def unexpected_error(
    request: Request, exception: Exception
) -> JSONResponse:
    logger.exception(
        "unexpected error %s %s", request.method, request.url.path
    )
    if not _is_api(request):
        raise exception
    return JSONResponse(
        status_code=500,
        content=_error_envelope(UNEXPECTED_MESSAGE, "INTERNAL_SERVER_ERROR"),
    )
