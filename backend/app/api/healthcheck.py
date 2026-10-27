from fastapi import APIRouter

from app.schemas.healthcheck import HealthcheckResponse

router = APIRouter(tags=["health"])


@router.get("/healthcheck", response_model=HealthcheckResponse)
async def healthcheck() -> HealthcheckResponse:
    """Liveness check; no database or provider calls."""
    return HealthcheckResponse()
