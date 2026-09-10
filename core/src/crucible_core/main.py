from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from .core.config import load_settings
from .core.database import upgrade
from .infrastructure.git import final_capture_worker as capture_worker
from .responses.core import (
    ProblemError,
    api_error,
    problem_error,
    unexpected_error,
    validation_error,
)
from .routes.admissions import Composition
from .routes.admissions import router as admissions_router
from .routes.system import router as system_router
from .services.admissions import reconcile_incomplete_admissions
from .services.finalizations import recover_finalizations


def build_capture_runner() -> capture_worker.ProcessCaptureRunner:
    """Compose a fresh runner per lifespan, shared via app.state."""
    return capture_worker.ProcessCaptureRunner()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    composition = getattr(app.state, "composition", None)
    # The app owns exactly one runner per lifespan: the composed
    # test runner when the factory provided one, else a fresh
    # process runner. It is shut down below, so every app controls
    # and reaps its own runner.
    runner = (
        composition.capture_runner
        if composition is not None and composition.capture_runner is not None
        else build_capture_runner()
    )
    app.state.capture_runner = runner
    app.state.settings = settings
    upgrade(settings.database_path)
    reconcile_incomplete_admissions(
        settings.database_path, capture_runner=runner
    )
    recover_finalizations(settings.database_path, capture_runner=runner)
    try:
        yield
    finally:
        try:
            runner.shutdown()
        except Exception:
            pass


def build_app(
    composition: Composition | None = None, **overrides: Any
) -> FastAPI:
    """Create an app with immutable per-app dependency composition.

    Production serves the module-level ``app`` (default
    composition). Tests build their own app with hooks, clocks, or
    a fake runner composed up front, either as a ``Composition`` or
    as keyword overrides; the composition is stored once and never
    mutated during requests, so injections cannot leak between
    concurrent requests or lifecycles.
    """
    if overrides:
        composition = replace(composition or Composition(), **overrides)
    constructed = FastAPI(lifespan=lifespan)
    constructed.state.composition = composition or Composition()
    constructed.add_exception_handler(StarletteHTTPException, api_error)
    constructed.add_exception_handler(ProblemError, problem_error)
    constructed.add_exception_handler(RequestValidationError, validation_error)
    constructed.add_exception_handler(Exception, unexpected_error)
    constructed.include_router(system_router, prefix="/v1")
    constructed.include_router(admissions_router, prefix="/v1")
    return constructed


app = build_app()
