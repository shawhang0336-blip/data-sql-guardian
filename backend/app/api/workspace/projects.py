"""Workspace API router: SQL repository import, script chunks, DDL schemas, and lineage DAG."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.common import get_db
from . import projects_service

router = APIRouter(prefix="/projects", tags=["workspace"])


class ImportSacRequest(BaseModel):
    name: Optional[str] = None


class ImportDirRequest(BaseModel):
    name: str
    dir_path: str
    dialect: str = "auto"


@router.get("")
def list_projects(db: Session = Depends(get_db)):
    return {"projects": projects_service.list_all_projects(db)}


@router.post("/import-sac")
def import_sac_corpus(payload: ImportSacRequest = ImportSacRequest(), db: Session = Depends(get_db)):
    try:
        return projects_service.load_sac_project(db, name=payload.name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/import-dir")
def import_local_dir(payload: ImportDirRequest, db: Session = Depends(get_db)):
    try:
        return projects_service.load_directory_project(
            db,
            name=payload.name,
            dir_path=payload.dir_path,
            dialect=payload.dialect,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/import-zip")
async def import_zip_archive(
    file: UploadFile = File(...),
    name: str = Form("ZIP 导入项目"),
    dialect: str = Form("auto"),
    db: Session = Depends(get_db),
):
    content = await file.read()
    try:
        return projects_service.load_zip_project(
            db,
            name=name,
            zip_bytes=content,
            dialect=dialect,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{project_id}")
def get_project(project_id: str, db: Session = Depends(get_db)):
    detail = projects_service.get_project_detail(db, project_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"项目不存在: {project_id}")
    return detail


@router.delete("/{project_id}")
def remove_project(project_id: str, db: Session = Depends(get_db)):
    ok = projects_service.delete_project(db, project_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"项目不存在: {project_id}")
    return {"deleted": True, "project_id": project_id}


@router.get("/{project_id}/scripts")
def list_scripts(project_id: str, db: Session = Depends(get_db)):
    return {"scripts": projects_service.list_project_scripts(db, project_id)}


@router.get("/{project_id}/scripts/{script_id}")
def get_script(project_id: str, script_id: str, db: Session = Depends(get_db)):
    detail = projects_service.get_script_detail(db, project_id, script_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"脚本不存在: {script_id}")
    return detail


@router.get("/{project_id}/schemas")
def list_schemas(project_id: str, db: Session = Depends(get_db)):
    return {"schemas": projects_service.list_project_schemas(db, project_id)}


@router.get("/{project_id}/lineage")
def get_lineage(project_id: str, db: Session = Depends(get_db)):
    res = projects_service.read_project_lineage(db, project_id)
    if res is None:
        raise HTTPException(status_code=404, detail=f"项目不存在: {project_id}")
    return res


@router.post("/{project_id}/lineage/rebuild")
def rebuild_lineage(project_id: str, db: Session = Depends(get_db)):
    res = projects_service.rebuild_lineage_for_project(db, project_id)
    if res is None:
        raise HTTPException(status_code=404, detail=f"项目不存在: {project_id}")
    return res
