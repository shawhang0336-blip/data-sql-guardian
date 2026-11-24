"""Verify dialect chunker, DDL parser, Zip Slip guard, and real SAC cross-script lineage topology."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.catalog import get_project_detail, get_script_detail, import_sac_sample_project, list_project_schemas
from app.infra.db import Base
from app.infra.db_tables import LineageEdgeModel
from app.ingestion import (
    analyze_chunk_ast,
    chunk_sql_script,
    extract_zip_safely,
    parse_ddl_statements,
)

SQL_EXPLORER_DIR = Path(__file__).resolve().parents[3] / "sql_explorer"
SAC_SAMPLES_DIR = SQL_EXPLORER_DIR / "sample_scripts"


def _new_test_session(tmp_path: Path):
    db_path = tmp_path / "test_lineage.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def test_ddl_parser_handles_oracle_byte_and_storage_clauses():
    sample_ddl = """
    CREATE TABLE "SACCNUA1"."SF_SAC_BASE_ACCOUNT_INFO" (
      "ACCOUNTNO" VARCHAR2(64 BYTE) NOT NULL ENABLE,
      "AMT" NUMBER(18, 2) DEFAULT (0),
      "CORP_FLAG" CHAR(1 BYTE),
      CONSTRAINT "PK_SAC_BASE_ACCT" PRIMARY KEY ("ACCOUNTNO")
    ) SEGMENT CREATION IMMEDIATE PCTFREE 10 TABLESPACE "NSAFE" ;
    """
    records = parse_ddl_statements(sample_ddl)
    assert len(records) == 1
    rec = records[0]
    assert rec.table_name == "SF_SAC_BASE_ACCOUNT_INFO"
    assert rec.schema_name == "SACCNUA1"
    assert rec.primary_keys == ["ACCOUNTNO"]
    col_names = [c["name"] for c in rec.columns]
    assert col_names == ["ACCOUNTNO", "AMT", "CORP_FLAG"]


def test_chunker_tracks_lines_control_context_and_unwraps_execute_immediate():
    plsql = """CREATE OR REPLACE PROCEDURE SP_DEMO IS
BEGIN
  IF TO_CHAR(SYSDATE, 'HH24') < 13 THEN
    EXECUTE IMMEDIATE 'TRUNCATE TABLE SAC_DEMO_TMP';
  END IF;
  INSERT INTO SAC_DEST (ID) SELECT ID FROM SAC_SRC;
  COMMIT;
END;
"""
    parsed = chunk_sql_script(plsql, filename="SP_DEMO.sql", dialect="oracle")
    assert parsed.file_kind == "PROCEDURE"
    assert parsed.commit_count == 1
    assert parsed.has_exception_handler is False
    dyn_chunks = [c for c in parsed.chunks if c.is_dynamic_sql]
    assert len(dyn_chunks) == 1
    assert dyn_chunks[0].statement_kind == "TRUNCATE"
    assert "TO_CHAR(SYSDATE" in dyn_chunks[0].control_context
    assert dyn_chunks[0].start_line == 4
    feat = analyze_chunk_ast(dyn_chunks[0].effective_sql, statement_kind=dyn_chunks[0].statement_kind, dialect="oracle")
    assert "SAC_DEMO_TMP" in feat.target_tables


def test_zip_slip_traversal_is_blocked(tmp_path: Path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../evil.sql", "SELECT 1 FROM DUAL;")
    with pytest.raises(ValueError, match="路径穿越"):
        extract_zip_safely(buf.getvalue(), tmp_path / "extract")


@pytest.mark.skipif(not SAC_SAMPLES_DIR.exists(), reason="Real SAC sample scripts not found on machine")
def test_real_sac_corpus_import_chunks_schemas_and_lineage(tmp_path: Path):
    Session = _new_test_session(tmp_path)
    with Session() as db:
        proj = import_sac_sample_project(db, sql_explorer_dir=str(SQL_EXPLORER_DIR))
        assert proj.script_count == 25
        assert proj.chunk_count > 100
        assert proj.table_count >= 10

        detail = get_project_detail(db, proj.id)
        assert detail is not None
        filenames = {s["filename"] for s in detail["scripts"]}
        assert "SAC_TABLE_SCHEMAS.sql" in filenames
        assert "SP_SAC_ACCT_AMOUNT.sql" in filenames
        assert "SP_SAC_LARGE_BULU.sql" in filenames
        assert "SP_SAC_JGDEJSH_TMP.sql" in filenames

        acct_script = next(s for s in detail["scripts"] if s["filename"] == "SP_SAC_ACCT_AMOUNT.sql")
        sd = get_script_detail(db, proj.id, acct_script["id"])
        assert len(sd["chunks"]) >= 5
        assert all(c["start_line"] >= 1 and c["end_line"] >= c["start_line"] for c in sd["chunks"])

        schemas = list_project_schemas(db, proj.id)
        assert len(schemas) >= 10

        edges = db.query(LineageEdgeModel).filter(LineageEdgeModel.project_id == proj.id).all()
        assert len(edges) >= 1
        edge_kinds = {e.edge_kind for e in edges}
        assert "DATA_FLOW" in edge_kinds or "TRUNCATE_CONFLICT" in edge_kinds or "WRITE_CONFLICT" in edge_kinds
