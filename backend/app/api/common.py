"""Shared API dependencies and exception-to-HTTP helpers."""

from __future__ import annotations

from typing import Iterator

from sqlalchemy.orm import Session

from app.infra.db import SessionLocal


def get_db() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
