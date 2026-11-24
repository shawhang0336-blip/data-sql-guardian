"""Project import, SAC corpus loader, schema indexing, and script/chunk catalog service."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, func
from sqlalchemy.orm import Session

from app.infra.blob_store import blob_store
from app.infra.config import ROOT_DIR, get_settings
from app.infra.db_tables import (
    OptimizationFindingModel,
    ScriptChunkModel,
    ScriptFileModel,
    ScriptProjectModel,
    ScriptRevisionModel,
    TableSchemaModel,
)
from app.ingestion import (
    CollectedScript,
    aggregate_script_features,
    analyze_chunk_ast,
    chunk_sql_script,
    collect_directory_scripts,
    collect_real_sac_scripts,
    extract_zip_safely,
    parse_ddl_statements,
)
from .lineage_graph import rebuild_project_lineage


def ingest_collected_scripts(
    session: Session,
    *,
    name: str,
    source_mode: str,
    source_path: str,
    dialect: str,
    collected: List[CollectedScript],
) -> ScriptProjectModel:
    """Persist imported scripts, SQL chunks, AST features, DDL schemas, v1 revisions, and lineage."""
    if not collected:
        raise ValueError("未在导入源中发现有效的 SQL/DDL 脚本文件")

    project = ScriptProjectModel(
        id=str(uuid.uuid4()),
        name=name,
        source_mode=source_mode,
        source_path=source_path,
        dialect=dialect,
        status="READY",
        script_count=len(collected),
        chunk_count=0,
        table_count=0,
        finding_count=0,
    )
    session.add(project)
    session.flush()

    total_chunks = 0
    seen_tables: Dict[str, TableSchemaModel] = {}

    for item in collected:
        script_id = str(uuid.uuid4())
        blob_uri = blob_store.put_text(
            item.content,
            subdir=f"projects/{project.id}",
            filename=item.filename,
        )
        eff_dialect = None if dialect in ("auto", "") else dialect
        parsed = chunk_sql_script(item.content, filename=item.filename, dialect=eff_dialect)

        chunk_features = []
        chunk_models: List[ScriptChunkModel] = []
        for ch in parsed.chunks:
            feat = analyze_chunk_ast(
                ch.effective_sql,
                statement_kind=ch.statement_kind,
                dialect=parsed.dialect,
            )
            chunk_features.append(feat)
            chunk_models.append(
                ScriptChunkModel(
                    id=str(uuid.uuid4()),
                    project_id=project.id,
                    script_id=script_id,
                    chunk_index=ch.chunk_index,
                    start_line=ch.start_line,
                    end_line=ch.end_line,
                    statement_kind=ch.statement_kind,
                    is_dynamic_sql=ch.is_dynamic_sql,
                    control_context=ch.control_context,
                    target_tables_json=feat.target_tables,
                    source_tables_json=feat.source_tables,
                    ast_features_json=feat.to_dict(),
                    ast_fingerprint=feat.ast_fingerprint,
                    raw_sql=ch.raw_sql,
                    comments_json=ch.comments,
                )
            )

        agg = aggregate_script_features(chunk_features)
        script_row = ScriptFileModel(
            id=script_id,
            project_id=project.id,
            filename=item.filename,
            relative_path=item.relative_path,
            dialect=parsed.dialect,
            file_kind=parsed.file_kind,
            sha256=item.sha256,
            blob_uri=blob_uri,
            line_count=parsed.line_count,
            chunk_count=len(chunk_models),
            target_tables_json=agg["target_tables"],
            source_tables_json=agg["source_tables"],
            ast_fingerprint=agg["ast_fingerprint"],
            has_exception_handler=parsed.has_exception_handler,
            commit_count=parsed.commit_count,
            current_version=1,
            status="ACTIVE",
        )
        session.add(script_row)
        session.flush()
        for cm in chunk_models:
            session.add(cm)
        total_chunks += len(chunk_models)

        session.add(
            ScriptRevisionModel(
                id=str(uuid.uuid4()),
                project_id=project.id,
                script_id=script_id,
                version=1,
                blob_uri=blob_uri,
                sha256=item.sha256,
                author="system_import",
                change_summary="初始基线导入",
            )
        )

        # Parse DDL tables if file is DDL or contains CREATE TABLE definitions
        if parsed.file_kind == "DDL" or "CREATE TABLE" in item.content.upper():
            for rec in parse_ddl_statements(item.content):
                tbl_key = rec.table_name
                if tbl_key not in seen_tables:
                    ts = TableSchemaModel(
                        id=str(uuid.uuid4()),
                        project_id=project.id,
                        table_name=rec.table_name,
                        schema_name=rec.schema_name,
                        columns_json=rec.columns,
                        primary_keys_json=rec.primary_keys,
                        indexes_json=rec.indexes,
                        source_script_id=script_id,
                    )
                    seen_tables[tbl_key] = ts
                    session.add(ts)
                else:
                    existing = seen_tables[tbl_key]
                    if rec.columns and not existing.columns_json:
                        existing.columns_json = rec.columns
                    if rec.primary_keys:
                        existing.primary_keys_json = list(
                            dict.fromkeys((existing.primary_keys_json or []) + rec.primary_keys)
                        )
                    if rec.indexes:
                        existing.indexes_json = (existing.indexes_json or []) + rec.indexes

    project.chunk_count = total_chunks
    project.table_count = len(seen_tables)
    session.commit()

    rebuild_project_lineage(session, project.id)
    session.refresh(project)
    return project


def import_project_from_zip(
    session: Session,
    *,
    name: str,
    zip_bytes: bytes,
    dialect: str = "auto",
) -> ScriptProjectModel:
    """Import a SQL repository from an uploaded ZIP archive."""
    tmp_dir = blob_store.root_dir / "uploads" / str(uuid.uuid4())
    collected = extract_zip_safely(zip_bytes, tmp_dir)
    return ingest_collected_scripts(
        session,
        name=name,
        source_mode="ZIP",
        source_path=str(tmp_dir),
        dialect=dialect,
        collected=collected,
    )


def import_project_from_directory(
    session: Session,
    *,
    name: str,
    dir_path: str,
    dialect: str = "auto",
) -> ScriptProjectModel:
    """Import SQL/DDL files recursively from a local directory path."""
    collected = collect_directory_scripts(dir_path)
    return ingest_collected_scripts(
        session,
        name=name,
        source_mode="LOCAL_DIR",
        source_path=str(Path(dir_path).expanduser().resolve()),
        dialect=dialect,
        collected=collected,
    )


def import_sac_sample_project(
    session: Session,
    *,
    name: str = "SAC 真实 Oracle ETL 资产库",
    sql_explorer_dir: Optional[str] = None,
) -> ScriptProjectModel:
    """One-click loader importing the 25 real SAC scripts from `sql_explorer/sample_scripts`."""
    base_dir = sql_explorer_dir or get_settings().sql_explorer_dir or str(ROOT_DIR.parent / "sql_explorer")
    samples_dir, collected = collect_real_sac_scripts(base_dir)
    return ingest_collected_scripts(
        session,
        name=name,
        source_mode="SAC_SAMPLE",
        source_path=str(samples_dir),
        dialect="oracle",
        collected=collected,
    )


def serialize_project(project: ScriptProjectModel) -> Dict[str, Any]:
    return {
        "id": project.id,
        "name": project.name,
        "source_mode": project.source_mode,
        "source_path": project.source_path,
        "dialect": project.dialect,
        "status": project.status,
        "script_count": project.script_count,
        "chunk_count": project.chunk_count,
        "table_count": project.table_count,
        "finding_count": project.finding_count,
        "summary": project.summary_json or {},
        "created_at": project.created_at.isoformat() if project.created_at else None,
        "updated_at": project.updated_at.isoformat() if project.updated_at else None,
    }


def serialize_script(script: ScriptFileModel, *, include_content: bool = False) -> Dict[str, Any]:
    data = {
        "id": script.id,
        "project_id": script.project_id,
        "filename": script.filename,
        "relative_path": script.relative_path,
        "dialect": script.dialect,
        "file_kind": script.file_kind,
        "sha256": script.sha256,
        "line_count": script.line_count,
        "chunk_count": script.chunk_count,
        "target_tables": script.target_tables_json or [],
        "source_tables": script.source_tables_json or [],
        "ast_fingerprint": script.ast_fingerprint,
        "has_exception_handler": script.has_exception_handler,
        "commit_count": script.commit_count,
        "current_version": script.current_version,
        "status": script.status,
        "updated_at": script.updated_at.isoformat() if script.updated_at else None,
    }
    if include_content:
        data["content"] = load_script_raw_text(script)
    return data


def load_script_raw_text(script: ScriptFileModel) -> str:
    try:
        return blob_store.get_text(script.blob_uri)
    except FileNotFoundError:
        return ""


def list_projects(session: Session) -> List[Dict[str, Any]]:
    rows = session.query(ScriptProjectModel).order_by(ScriptProjectModel.created_at.desc()).all()
    return [serialize_project(r) for r in rows]


def get_project_detail(session: Session, project_id: str) -> Optional[Dict[str, Any]]:
    proj = session.get(ScriptProjectModel, project_id)
    if proj is None:
        return None
    data = serialize_project(proj)
    scripts = (
        session.query(ScriptFileModel)
        .filter(ScriptFileModel.project_id == project_id)
        .order_by(ScriptFileModel.filename.asc())
        .all()
    )
    finding_counts = dict(
        session.query(OptimizationFindingModel.script_id, func.count(OptimizationFindingModel.id))
        .filter(OptimizationFindingModel.project_id == project_id, OptimizationFindingModel.script_id.isnot(None))
        .group_by(OptimizationFindingModel.script_id)
        .all()
    )
    script_items = []
    for s in scripts:
        item = serialize_script(s)
        item["finding_count"] = finding_counts.get(s.id, 0)
        script_items.append(item)
    data["scripts"] = script_items
    return data


def list_project_scripts(session: Session, project_id: str) -> List[Dict[str, Any]]:
    scripts = (
        session.query(ScriptFileModel)
        .filter(ScriptFileModel.project_id == project_id)
        .order_by(ScriptFileModel.filename.asc())
        .all()
    )
    return [serialize_script(s) for s in scripts]


def get_script_detail(session: Session, project_id: str, script_id: str) -> Optional[Dict[str, Any]]:
    script = (
        session.query(ScriptFileModel)
        .filter(ScriptFileModel.project_id == project_id, ScriptFileModel.id == script_id)
        .first()
    )
    if script is None:
        return None
    chunks = (
        session.query(ScriptChunkModel)
        .filter(ScriptChunkModel.script_id == script_id)
        .order_by(ScriptChunkModel.chunk_index.asc())
        .all()
    )
    revisions = (
        session.query(ScriptRevisionModel)
        .filter(ScriptRevisionModel.script_id == script_id)
        .order_by(ScriptRevisionModel.version.desc())
        .all()
    )
    out = serialize_script(script, include_content=True)
    out["chunks"] = [
        {
            "id": c.id,
            "chunk_index": c.chunk_index,
            "start_line": c.start_line,
            "end_line": c.end_line,
            "statement_kind": c.statement_kind,
            "is_dynamic_sql": c.is_dynamic_sql,
            "control_context": c.control_context,
            "target_tables": c.target_tables_json or [],
            "source_tables": c.source_tables_json or [],
            "ast_features": c.ast_features_json or {},
            "ast_fingerprint": c.ast_fingerprint,
            "raw_sql": c.raw_sql,
            "comments": c.comments_json or [],
        }
        for c in chunks
    ]
    out["revisions"] = [
        {
            "id": r.id,
            "version": r.version,
            "sha256": r.sha256,
            "author": r.author,
            "change_summary": r.change_summary,
            "review_id": r.review_id,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in revisions
    ]
    return out


def list_project_schemas(session: Session, project_id: str) -> List[Dict[str, Any]]:
    rows = (
        session.query(TableSchemaModel)
        .filter(TableSchemaModel.project_id == project_id)
        .order_by(TableSchemaModel.table_name.asc())
        .all()
    )
    return [
        {
            "id": r.id,
            "table_name": r.table_name,
            "schema_name": r.schema_name,
            "columns": r.columns_json or [],
            "primary_keys": r.primary_keys_json or [],
            "indexes": r.indexes_json or [],
        }
        for r in rows
    ]


def delete_project(session: Session, project_id: str) -> bool:
    proj = session.get(ScriptProjectModel, project_id)
    if proj is None:
        return False
    session.delete(proj)
    session.commit()
    return True
