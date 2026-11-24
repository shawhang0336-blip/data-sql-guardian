"""Platform API subpackage."""

from fastapi import APIRouter

from .settings import router as settings_router
from .system import router as system_router

router = APIRouter()
router.include_router(system_router)
router.include_router(settings_router)

__all__ = ["router"]
