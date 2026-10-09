"""System routes are deliberately separate from product routes."""
from fastapi import APIRouter
from ..config import settings
from ..schemas.common import HealthResponse
from ..services.health import database_ready

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/ready", response_model=HealthResponse)
def ready() -> HealthResponse:
    database_ready()
    return HealthResponse(status="ok", service=settings.app_name, database="ok")
