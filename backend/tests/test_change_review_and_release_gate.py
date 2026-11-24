"""Verify pre-release change evaluator: AST Diff, multi-hop blast radius, BLOCK/WARN/PASS gate, Interrupt idempotency, baseline merge, and hash trace."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.catalog import get_script_detail, ingest_collected_scripts
from app.infra.db import Base
from app.infra.db_tables import ScriptFileModel, ScriptRevisionModel
from app.ingestion import CollectedScript
from app.review import (
    approve_change_review,
    evaluate_script_change,
    export_review_markdown,
    get_review_run_trace,
    reject_change_review,
)


def _new_test_session(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'review_gate.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _script_item(name: str, sql: str) -> CollectedScript:
    return CollectedScript(
        filename=name,
        relative_path=name,
        content=sql,
        sha256=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
    )


def _seed_three_hop_project(db):
    s1 = """CREATE OR REPLACE PROCEDURE SP_STAGE_1 IS
BEGIN
  INSERT INTO MID_A (ID, AMT) SELECT ID, AMT FROM RAW_TXN WHERE AMT > 0;
  COMMIT;
EXCEPTION WHEN OTHERS THEN ROLLBACK; RAISE;
END;"""
    s2 = """CREATE OR REPLACE PROCEDURE SP_STAGE_2 IS
BEGIN
  INSERT INTO DM_B (ID, AMT) SELECT ID, AMT FROM MID_A WHERE AMT > 0;
  COMMIT;
EXCEPTION WHEN OTHERS THEN ROLLBACK; RAISE;
END;"""
    s3 = """CREATE OR REPLACE PROCEDURE SP_STAGE_3 IS
BEGIN
  INSERT INTO RPT_C (ID, AMT) SELECT ID, AMT FROM DM_B WHERE AMT > 0;
  COMMIT;
EXCEPTION WHEN OTHERS THEN ROLLBACK; RAISE;
END;"""
    return ingest_collected_scripts(
        db,
        name="Three-Hop ETL",
        source_mode="LOCAL_DIR",
        source_path="/tmp/demo",
        dialect="oracle",
        collected=[
            _script_item("SP_STAGE_1.sql", s1),
            _script_item("SP_STAGE_2.sql", s2),
            _script_item("SP_STAGE_3.sql", s3),
        ],
    )


def test_modify_introducing_p0_triggers_block_and_traces_two_hop_blast_radius(tmp_path: Path):
    Session = _new_test_session(tmp_path)
    with Session() as db:
        proj = _seed_three_hop_project(db)
        s1 = db.query(ScriptFileModel).filter(ScriptFileModel.project_id == proj.id, ScriptFileModel.filename == "SP_STAGE_1.sql").first()

        # Modify removing EXCEPTION handler and adding unbounded UPDATE (S002 P0 + S004 P0 + exception removed)
        bad_new_sql = """CREATE OR REPLACE PROCEDURE SP_STAGE_1 IS
BEGIN
  UPDATE MID_A SET AMT = 0;
  INSERT INTO MID_A (ID, AMT) SELECT ID, AMT FROM RAW_TXN WHERE AMT > 0;
  COMMIT;
END;"""
        rev = evaluate_script_change(
            db,
            proj.id,
            change_type="MODIFY",
            filename="SP_STAGE_1.sql",
            target_script_id=s1.id,
            new_content=bad_new_sql,
            author="qa_tester",
            change_summary="误删 WHERE 与异常处理",
            run_llm=False,
        )
        assert rev["deterministic_verdict"] == "BLOCK"
        assert rev["final_verdict"] == "BLOCK"
        assert rev["status"] == "WAITING_APPROVAL"
        assert rev["blast_radius"]["total_impacted_scripts"] == 2
        assert rev["blast_radius"]["max_hop"] == 2
        impacted_files = [s["filename"] for s in rev["blast_radius"]["impacted_scripts"]]
        assert impacted_files == ["SP_STAGE_2.sql", "SP_STAGE_3.sql"]
        assert rev["diff_summary"]["exception_handler_removed"] is True

        md = export_review_markdown(db, rev["id"])
        assert "BLOCK" in md and "SP_STAGE_2.sql" in md

        trace = get_review_run_trace(db, rev["run_id"])
        assert trace is not None
        assert trace["chain_verified"] is True
        kinds = [e["kind"] for e in trace["events"]]
        assert kinds == ["RUN_CREATED", "AST_DIFF_COMPUTED", "GATE_EVALUATED", "RELEASE_APPROVAL_REQUIRED"]


def test_approve_merge_baseline_and_interrupt_idempotency(tmp_path: Path):
    Session = _new_test_session(tmp_path)
    with Session() as db:
        proj = _seed_three_hop_project(db)
        s1 = db.query(ScriptFileModel).filter(ScriptFileModel.project_id == proj.id, ScriptFileModel.filename == "SP_STAGE_1.sql").first()

        clean_v2 = """CREATE OR REPLACE PROCEDURE SP_STAGE_1 IS
BEGIN
  INSERT INTO MID_A (ID, AMT) SELECT ID, AMT FROM RAW_TXN WHERE AMT >= 10;
  COMMIT;
EXCEPTION WHEN OTHERS THEN ROLLBACK; RAISE;
END;"""
        rev = evaluate_script_change(
            db,
            proj.id,
            change_type="MODIFY",
            filename="SP_STAGE_1.sql",
            target_script_id=s1.id,
            new_content=clean_v2,
            author="alice",
            change_summary="调整过滤阈值",
        )
        assert rev["deterministic_verdict"] == "WARN"  # hits downstream 2 scripts
        first_ok = approve_change_review(db, rev["id"], reviewer="bob", comment="批准", merge_baseline=True)
        assert first_ok["status"] == "APPROVED"
        assert first_ok["merged_to_baseline"] is True
        assert first_ok["idempotent_replay"] is False

        # Exact same answer must replay idempotently without bumping revision again
        replay = approve_change_review(db, rev["id"], reviewer="bob", comment="批准", merge_baseline=True)
        assert replay["idempotent_replay"] is True

        # Rejecting an already-approved review must fail cleanly
        with pytest.raises(ValueError, match="不可驳回"):
            reject_change_review(db, rev["id"], reviewer="bob", comment="撤回")

        sd = get_script_detail(db, proj.id, s1.id)
        assert sd["current_version"] == 2
        revs = db.query(ScriptRevisionModel).filter(ScriptRevisionModel.script_id == s1.id).all()
        assert len(revs) == 2

        trace = get_review_run_trace(db, rev["run_id"])
        assert trace["chain_verified"] is True
        assert trace["events"][-1]["kind"] == "RELEASE_APPROVED"
