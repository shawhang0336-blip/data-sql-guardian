"""Workspace domain service adapting catalog operations cleanly for API routers."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.catalog import (
    delete_project,
    get_project_detail,
    get_script_detail,
    import_project_from_directory,
    import_project_from_zip,
    import_sac_sample_project,
    list_project_schemas,
    list_project_scripts,
    list_projects,
    rebuild_project_lineage,
    serialize_project,
)
from app.infra.db_tables import LineageEdgeModel, ScriptFileModel, ScriptProjectModel


def list_all_projects(db: Session) -> List[Dict[str, Any]]:
    return list_projects(db)


def load_sac_project(db: Session, *, name: Optional[str] = None) -> Dict[str, Any]:
    proj = import_sac_sample_project(db, name=name or "SAC 真实 Oracle ETL 资产库")
    return get_project_detail(db, proj.id) or serialize_project(proj)


def load_directory_project(
    db: Session,
    *,
    name: str,
    dir_path: str,
    dialect: str = "auto",
) -> Dict[str, Any]:
    proj = import_project_from_directory(db, name=name, dir_path=dir_path, dialect=dialect)
    return get_project_detail(db, proj.id) or serialize_project(proj)


def load_zip_project(
    db: Session,
    *,
    name: str,
    zip_bytes: bytes,
    dialect: str = "auto",
) -> Dict[str, Any]:
    proj = import_project_from_zip(db, name=name, zip_bytes=zip_bytes, dialect=dialect)
    return get_project_detail(db, proj.id) or serialize_project(proj)


def read_project_lineage(db: Session, project_id: str) -> Optional[Dict[str, Any]]:
    proj = db.get(ScriptProjectModel, project_id)
    if proj is None:
        return None
    scripts = db.query(ScriptFileModel).filter(ScriptFileModel.project_id == project_id).all()
    name_map = {s.id: s.filename for s in scripts}
    edges = db.query(LineageEdgeModel).filter(LineageEdgeModel.project_id == project_id).all()
    return {
        "project_id": project_id,
        "nodes": [
            {
                "id": s.id,
                "filename": s.filename,
                "file_kind": s.file_kind,
                "target_tables": s.target_tables_json or [],
                "source_tables": s.source_tables_json or [],
            }
            for s in scripts
        ],
        "edges": [
            {
                "id": e.id,
                "upstream_script_id": e.upstream_script_id,
                "upstream_filename": name_map.get(e.upstream_script_id, e.upstream_script_id),
                "downstream_script_id": e.downstream_script_id,
                "downstream_filename": name_map.get(e.downstream_script_id, e.downstream_script_id),
                "via_table": e.via_table,
                "edge_kind": e.edge_kind,
            }
            for e in edges
        ],
        "summary": proj.summary_json or {},
    }


def rebuild_lineage_for_project(db: Session, project_id: str) -> Optional[Dict[str, Any]]:
    if db.get(ScriptProjectModel, project_id) is None:
        return None
    rebuild_project_lineage(db, project_id)
    return read_project_lineage(db, project_id)


__all__ = [
    "delete_project",
    "get_project_detail",
    "get_script_detail",
    "list_all_projects",
    "list_project_schemas",
    "list_project_scripts",
    "load_directory_project",
    "load_sac_project",
    "load_zip_project",
    "read_project_lineage",
    "rebuild_lineage_for_project",
]
