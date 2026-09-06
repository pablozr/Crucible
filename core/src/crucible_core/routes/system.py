from fastapi import APIRouter, Request

from crucible_core.schemas.system import HealthResponse, OperationalStatusResponse
from crucible_core.services.system import operational_status
from crucible_core.version import VERSION

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", version=VERSION, api_version="v1")


@router.get("/status", response_model=OperationalStatusResponse)
def status(request: Request) -> OperationalStatusResponse:
    return OperationalStatusResponse.model_validate(operational_status(request.app.state.settings))
