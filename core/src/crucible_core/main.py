from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from .core.config import load_settings
from .core.database import upgrade
from .responses.core import (
    ProblemError,
    api_error,
    problem_error,
    unexpected_error,
    validation_error,
)
from .routes.admissions import router as admissions_router
from .routes.system import router as system_router
from .services.admissions import reconcile_incomplete_admissions
from .services.finalizations import recover_finalizations


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    upgrade(settings.database_path)
    reconcile_incomplete_admissions(settings.database_path)
    recover_finalizations(settings.database_path)
    app.state.settings = settings
    yield


app = FastAPI(lifespan=lifespan)
app.add_exception_handler(StarletteHTTPException, api_error)
app.add_exception_handler(ProblemError, problem_error)
app.add_exception_handler(RequestValidationError, validation_error)
app.add_exception_handler(Exception, unexpected_error)
app.include_router(system_router, prefix="/v1")
app.include_router(admissions_router, prefix="/v1")
