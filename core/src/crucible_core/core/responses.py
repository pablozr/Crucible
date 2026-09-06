from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


async def api_error(
    request: Request, exception: StarletteHTTPException
) -> JSONResponse:
    if request.url.path.startswith("/v1/") and exception.status_code in {
        404,
        405,
    }:
        is_not_found = exception.status_code == 404

        return JSONResponse(
            status_code=exception.status_code,
            media_type="application/problem+json",
            content={
                "type": "about:blank",
                "title": "API route not found"
                if is_not_found
                else "API method not allowed",
                "status": exception.status_code,
                "detail": (
                    "No API route matches the request."
                    if is_not_found
                    else "The API route does not allow this method."
                ),
                "instance": request.url.path,
                "code": "API_ROUTE_NOT_FOUND"
                if is_not_found
                else "API_METHOD_NOT_ALLOWED",
            },
        )

    return JSONResponse(
        status_code=exception.status_code, content={"detail": exception.detail}
    )
