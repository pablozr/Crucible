from fastapi import APIRouter, Request

from crucible_core.schemas.envelope import (
    HealthData,
    ResponseEnvelope,
    SystemData,
)
from crucible_core.schemas.system import (
    HealthResponse,
    OperationalStatusResponse,
)
from crucible_core.services.system import operational_status
from crucible_core.version import VERSION

router = APIRouter()


@router.get("/health", response_model=ResponseEnvelope[HealthData])
def health() -> ResponseEnvelope[HealthData]:
    body = HealthResponse(status="ok", version=VERSION, api_version="v1")
    return ResponseEnvelope(
        status="ok",
        message="Service is healthy.",
        data=HealthData(health=body),
    )


@router.get("/status", response_model=ResponseEnvelope[SystemData])
def status(request: Request) -> ResponseEnvelope[SystemData]:
    body = OperationalStatusResponse.model_validate(
        operational_status(request.app.state.settings)
    )
    return ResponseEnvelope(
        status="ok",
        message="Service status retrieved.",
        data=SystemData(system=body),
    )
