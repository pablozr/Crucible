from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.exceptions import HTTPException as StarletteHTTPException

from .core.config import load_settings
from .core.database import upgrade
from .core.responses import api_error
from .routes.system import router as system_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    upgrade(settings.database_path)
    app.state.settings = settings
    yield


app = FastAPI(lifespan=lifespan)
app.add_exception_handler(StarletteHTTPException, api_error)
app.include_router(system_router, prefix="/v1")
