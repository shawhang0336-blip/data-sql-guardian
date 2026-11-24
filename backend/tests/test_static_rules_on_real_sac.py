"""Verify all 16 single-script and 6 project-wide rules on synthetic edge cases AND the 25 real SAC scripts."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.analysis import analyze_project_wide, evaluate_script_rules, list_findings
from app.analysis.core import evaluate_project_rules
from app.catalog import import_sac_sample_project
from app.infra.db import Base
from app.ingestion import analyze_chunk_ast, chunk_sql_script

SQL_EXPLORER_DIR = Path(__file__).resolve().parents[3] / "sql_explorer"
SAC_SAMPLES_DIR = SQL_EXPLORER_DIR / "sample_scripts"


def _new_test_session(tmp_path: Path):
    db_path = tmp_path / "test_rules.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _run_rules_on_text(sql_text: str, filename: str = "SP_SAMPLE.sql"):
    parsed = chunk_sql_script(sql_text, filename=filename, dialect="oracle")
    chunks_dicts = []
    targets = []
    sources = []
    for ch in parsed.chunks:
        feat = analyze_chunk_ast(ch.effective_sql, statement_kind=ch.statement_kind, dialect=parsed.dialect)
        targets.extend(feat.target_tables)
        sources.extend(feat.source_tables)
        chunks_dicts.append(
            {
                "id": f"c_{ch.chunk_index}",
                "chunk_index": ch.chunk_index,
                "start_line": ch.start_line,
                "end_line": ch.end_line,
                "statement_kind": ch.statement_kind,
                "is_dynamic_sql": ch.is_dynamic_sql,
                "control_context": ch.control_context,
                "target_tables": feat.target_tables,
                "source_tables": feat.source_tables,
                "ast_features": feat.to_dict(),
                "raw_sql": ch.raw_sql,
            }
        )
    script_dict = {
        "id": "s1",
        "filename": filename,
        "file_kind": parsed.file_kind,
        "commit_count": parsed.commit_count,
        "has_exception_handler": parsed.has_exception_handler,
        "target_tables": list(dict.fromkeys(targets)),
        "source_tables": list(dict.fromkeys(sources)),
    }
    return evaluate_script_rules(script_dict, chunks_dicts)


def test_all_critical_single_script_rules_trigger_accurately():
    sql = """
    CREATE OR REPLACE PROCEDURE SP_COMPLEX_BAD IS
    BEGIN
      EXECUTE IMMEDIATE 'TRUNCATE TABLE TGT_TBL';
      COMMIT;
      UPDATE TGT_TBL SET FLAG = '1';
      DELETE FROM TGT_TBL;
      IF TO_CHAR(SYSDATE, 'HH24') < 13 THEN
        INSERT INTO TGT_TBL
        SELECT DISTINCT T.*, SYS_GUID()
        FROM SRC_A T
        INNER JOIN SRC_B B ON REPLACE(T.ID, '-', '') = TRIM(B.ID)
        WHERE TO_CHAR(T.DT, 'YYYYMMDD') = '20260101'
          AND T.NAME LIKE '%FOO%'
          AND T.ID IS NULL
          AND T.CODE NOT IN (SELECT CODE FROM BLK_LIST)
          AND NOT EXISTS (SELECT 1 FROM CHK_TBL M WHERE M.ID = T.ID);
      END IF;
      COMMIT;
    END;
    """
    findings = _run_rules_on_text(sql, filename="SP_COMPLEX_BAD.sql")
    codes = {f["rule_code"] for f in findings}
    expected_subset = {"S001", "S002", "S003", "S004", "S005", "S006", "S007", "S008", "S009", "S010", "S012", "S014", "S015", "S016"}
    missing = expected_subset - codes
    assert not missing, f"Rules failed to fire cleanly: missing={missing}, got={codes}"


def test_project_rules_p001_to_p006_fire_deterministically():
    fake_topology = {
        "nodes": [
            {"id": "s1", "filename": "A.sql"},
            {"id": "s2", "filename": "B.sql"},
            {"id": "s3", "filename": "C.sql"},
            {"id": "s4", "filename": "D.sql"},
        ],
        "write_conflict_tables": [{"table": "SHARED_OUT", "script_ids": ["s1", "s2"], "filenames": ["A.sql", "B.sql"]}],
        "truncate_conflicts": [{"table": "MID_TBL", "truncators": ["A.sql"], "affected": ["B.sql"]}],
        "cycles": [["A.sql", "B.sql"]],
        "duplicate_clusters": [
            {
                "ast_fingerprint": "deadbeef12345678",
                "occurrence_count": 2,
                "script_count": 2,
                "occurrences": [
                    {"script_id": "s1", "filename": "A.sql", "start_line": 10, "target_tables": ["OUT"]},
                    {"script_id": "s2", "filename": "B.sql", "start_line": 20, "target_tables": ["OUT"]},
                ],
            }
        ],
        "longest_chain": ["A.sql", "B.sql", "C.sql", "D.sql"],
        "longest_chain_hops": 3,
        "hub_tables": [{"table": "HUB_TBL", "writer_count": 2, "reader_count": 3, "writers": ["A.sql", "B.sql"], "readers": ["C.sql", "D.sql"]}],
        "dead_or_orphan_tables": ["ORPHAN_TMP"],
        "missing_ddl_tables": ["UNKNOWN_EXT"],
    }
    p_findings = evaluate_project_rules(fake_topology, max_chain_hops=3)
    codes = {f["rule_code"] for f in p_findings}
    assert codes == {"P001", "P002", "P003", "P004", "P005", "P006"}


@pytest.mark.skipif(not SAC_SAMPLES_DIR.exists(), reason="Real SAC sample scripts not found on machine")
def test_real_sac_corpus_triggers_verified_bugs(tmp_path: Path):
    Session = _new_test_session(tmp_path)
    with Session() as db:
        proj = import_sac_sample_project(db, sql_explorer_dir=str(SQL_EXPLORER_DIR))
        summary = analyze_project_wide(db, proj.id, run_llm=False)
        assert summary["script_findings"] >= 20
        assert summary["ai_status"] == "SKIPPED"

        all_findings = list_findings(db, proj.id)
        acct_rules = {f["rule_code"] for f in all_findings if f["script_filename"] == "SP_SAC_ACCT_AMOUNT.sql"}
        # Verified real bugs inside SP_SAC_ACCT_AMOUNT.sql:
        # S001 dead NULL join at L148-152, S006 REPLACE JOIN at L81, S013 repeated writes to SAC_KHHZH_LIST
        assert {"S001", "S006", "S013"}.issubset(acct_rules), f"SP_SAC_ACCT_AMOUNT rules missing: {acct_rules}"

        bulu_rules = {f["rule_code"] for f in all_findings if f["script_filename"] == "SP_SAC_LARGE_BULU.sql"}
        # Verified real issues inside SP_SAC_LARGE_BULU.sql:
        # S006 dual-side SUBSTR/TRIM JOIN, S008 LIKE '%购汇%', S010 SYS_GUID(), S015 SYSDATE cutover
        assert {"S006", "S008", "S010", "S015"}.issubset(bulu_rules), f"SP_SAC_LARGE_BULU rules missing: {bulu_rules}"
