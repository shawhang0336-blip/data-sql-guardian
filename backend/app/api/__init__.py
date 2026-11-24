"""Layer 7 unified API router aggregator across all 4 functional domains."""

from fastapi import APIRouter

from .analysis import router as analysis_router
from .platform import router as platform_router
from .review import router as review_router
from .workspace import router as workspace_router

api_router = APIRouter(prefix="/api")
api_router.include_router(platform_router)
api_router.include_router(workspace_router)
api_router.include_router(analysis_router)
api_router.include_router(review_router)

__all__ = ["api_router"]
