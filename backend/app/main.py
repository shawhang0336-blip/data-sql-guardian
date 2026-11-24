"""Layer 8 FastAPI application composition root with database initialization and Run recovery."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import api_router
from app.infra.config import get_budget, get_settings
from app.infra.db import SessionLocal, init_db
from app.infra.llm import (
    close_http_client,
    configure_llm_concurrency,
    configure_llm_rate_limit,
)
from app.kernel import recover_interrupted_runs
from app.policy import project_many


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    with SessionLocal() as session:
        budget = get_budget(session)
        configure_llm_concurrency(int(budget.get("max_llm_concurrency", 4)))
        configure_llm_rate_limit(int(budget.get("max_llm_rpm", 60)))
        recover_interrupted_runs(
            session,
            active_statuses=("NEW", "RUNNING"),
            projector_many=project_many,
        )
    try:
        yield
    finally:
        close_http_client()


settings = get_settings()
origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]

app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(api_router)
