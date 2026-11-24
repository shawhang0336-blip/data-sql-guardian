"""Shared pytest fixtures isolating blob storage to per-test tmp_path."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.infra.blob_store import blob_store


@pytest.fixture(autouse=True)
def _isolated_blob_store(tmp_path: Path, monkeypatch):
    tmp_blobs = tmp_path / "blobs"
    tmp_blobs.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(blob_store, "root", tmp_blobs)
