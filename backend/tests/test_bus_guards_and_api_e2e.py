"""Verify 5 ds_bus adjudicators, BudgetGuard, scripted LLM workflows, and full FastAPI 4-domain endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.analysis import analyze_project_wide, analyze_single_script, list_findings
from app.api.common import get_db
from app.catalog import ingest_collected_scripts
from app.infra.db import Base
from app.infra.db_tables import ScriptFileModel
from app.ingestion import CollectedScript
from app.main import app
from app.policy import default_bus
from app.review import evaluate_script_change


class _ScriptedFakeLlm:
    """Deterministic fake Tool-Call model exercising every schema & ds_bus path without external calls."""

    def __init__(self, *, attempt_downgrade: bool = False, priority_limit: int = 1):
        self.calls = 0
        self.attempt_downgrade = attempt_downgrade
        self.priority_limit = priority_limit

    def _build_args(self, tool_name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if tool_name == "plan_script_review":
            allowed = payload.get("allowed_chunk_ids") or []
            return {
                "script_purpose": "清理并加载账户汇总明细",
                "priority_chunk_ids": allowed[: self.priority_limit],
                "review_focus": "关注 JOIN 函数包裹和 NULL 等值过滤",
            }
        if tool_name == "diagnose_chunk_issues":
            return {
                "chunk_id": payload["chunk_id"],
                "findings": [
                    {
                        "rule_code": "S006",
                        "severity": "P1_HIGH",
                        "category": "PERFORMANCE",
                        "root_cause": "连接键包裹 REPLACE/TRIM 导致索引不可用并产生 Hash Skew。",
                        "actionable_fix": "使用标准化后的清洗列直接等值关联。",
                        "expected_benefit": "预期缩短 JOIN 执行耗时 50% 以上。",
                        "false_positive": False,
                    }
                ],
            }
        if tool_name == "propose_sql_rewrite":
            tgt = (payload.get("required_target_tables") or ["TGT_TBL"])[0]
            return {
                "chunk_id": payload["chunk_id"],
                "rule_code": payload.get("rule_code", "S006"),
                "optimization_summary": "移除连接键函数包装改用标准化列关联",
                "rewritten_sql": f"INSERT INTO {tgt} (ID) SELECT A.ID FROM SRC_A A INNER JOIN SRC_B B ON A.CLEAN_ID = B.CLEAN_ID",
                "expected_benefit": "消除全表扫并恢复索引范围扫描。",
                "equivalence_notes": "要求 CLEAN_ID 在上游清洗阶段已完成标准化。",
            }
        if tool_name == "synthesize_project_optimizations":
            allowed_tbl = (payload.get("allowed_tables") or ["TGT_TBL"])[0]
            return {
                "executive_summary": "全仓建议优先治理共享主表并发写入冲突并统一标准清洗中间层。",
                "architecture_recommendations": [
                    {
                        "priority": "P1_HIGH",
                        "category": "ARCHITECTURE",
                        "title": "拆分枢纽写入表并发依赖",
                        "affected_tables": [allowed_tbl],
                        "affected_scripts": ["SP_A.sql"],
                        "proposal": "确立单主责写脚本并固化下游血缘拓扑顺序。",
                        "expected_benefit": "消除锁表等待并降低重跑异常率。",
                    }
                ],
            }
        if tool_name == "assess_change_risk":
            return {
                "risk_summary": "涉及核心表更新，需核对下游结果集行数。",
                "semantic_risks": ["存在全表更新/异常处理缺失风险"],
                "verification_sqls": ["SELECT COUNT(1) FROM TGT_TBL;"],
                "backfill_scripts": ["-- 无需额外历史回刷"],
                "rollback_plan": "从 ScriptRevisionModel v1 恢复原存储过程体后重跑当期批处理。",
            }
        if tool_name == "summarize_release_gate":
            if self.attempt_downgrade:
                return {
                    "recommended_verdict": "PASS",
                    "developer_notes": "试图降级",
                    "dba_notes": "ok",
                    "qa_notes": "ok",
                    "release_checklist": ["check"],
                }
            return {
                "recommended_verdict": payload.get("deterministic_verdict", "WARN"),
                "developer_notes": "建议按 Static Review 清单修复后再行发布。",
                "dba_notes": "关注目标表锁与分区索引覆盖。",
                "qa_notes": "验证上线前后行数及关键金额口径一致性。",
                "release_checklist": ["执行验证 SQL", "备齐回滚脚本"],
            }
        raise ValueError(f"Unknown tool_name: {tool_name}")

    def assign(
        self,
        tool_name: str,
        instruction: str,
        payload: Dict[str, Any],
        feedback: Any = None,
        timeout: Any = None,
    ):
        _ = (instruction, feedback, timeout)
        self.calls += 1
        args = self._build_args(tool_name, payload)
        trace = {
            "provider": "scripted",
            "model": "scripted-fake",
            "tool_name": tool_name,
            "elapsed_seconds": 0.01,
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        }
        return args, trace


def _new_test_session(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'api_e2e.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def test_ds_bus_5_adjudicators_block_hallucinations_bad_sql_and_downgrade():
    bus = default_bus()

    # 1. Fake chunk_id blocked
    reason = bus.adjudicate(
        "diagnose_chunk_issues",
        {"chunk_id": "phantom_chunk"},
        {"allowed_chunk_ids": ["real_chunk_1"]},
    )
    assert reason is not None and "切块幻觉" in reason

    # 2. Fake table name blocked in project synthesis
    reason = bus.adjudicate(
        "synthesize_project_optimizations",
        {
            "architecture_recommendations": [
                {"priority": "P1_HIGH", "category": "ARCHITECTURE", "affected_tables": ["FAKE_GHOST_TABLE"]}
            ]
        },
        {"allowed_tables": ["REAL_SAC_TBL"]},
    )
    assert reason is not None and "实体幻觉" in reason

    # 3. Bad SQL syntax or DROP TABLE blocked
    reason = bus.adjudicate(
        "propose_sql_rewrite",
        {"rewritten_sql": "DROP TABLE REAL_SAC_TBL;"},
        {"required_target_tables": ["REAL_SAC_TBL"]},
    )
    assert reason is not None and "DROP TABLE" in reason

    # 4. Missing original target table blocked
    reason = bus.adjudicate(
        "propose_sql_rewrite",
        {"rewritten_sql": "INSERT INTO OTHER_TBL (ID) SELECT ID FROM SRC_A"},
        {"required_target_tables": ["REAL_SAC_TBL"]},
    )
    assert reason is not None and "目标表语义丢失" in reason

    # 5. BLOCK gate downgrade blocked
    reason = bus.adjudicate(
        "summarize_release_gate",
        {"recommended_verdict": "PASS"},
        {"deterministic_verdict": "BLOCK"},
    )
    assert reason is not None and "上线门禁硬红线" in reason


def test_scripted_llm_single_script_project_and_gate_floor_end_to_end(tmp_path: Path):
    Session = _new_test_session(tmp_path)
    with Session() as db:
        sql_body = """CREATE OR REPLACE PROCEDURE SP_DEMO_OPT IS
BEGIN
  INSERT INTO TGT_TBL (ID)
  SELECT A.ID FROM SRC_A A INNER JOIN SRC_B B ON TRIM(A.ID) = REPLACE(B.ID, '-', '');
  COMMIT;
EXCEPTION WHEN OTHERS THEN ROLLBACK; RAISE;
END;"""
        proj = ingest_collected_scripts(
            db,
            name="LLM Guard Demo",
            source_mode="LOCAL_DIR",
            source_path="/tmp/demo",
            dialect="oracle",
            collected=[CollectedScript("SP_DEMO_OPT.sql", "SP_DEMO_OPT.sql", sql_body, "abc123")],
        )
        script = db.query(ScriptFileModel).filter(ScriptFileModel.project_id == proj.id).first()

        fake_llm = _ScriptedFakeLlm(attempt_downgrade=False, priority_limit=3)
        res_single = analyze_single_script(db, proj.id, script.id, run_llm=True, llm_client=fake_llm)
        assert res_single["ai_status"] == "COMPLETED"
        assert fake_llm.calls >= 2

        findings = list_findings(db, proj.id, script_id=script.id)
        refined = [f for f in findings if f["ai_refined"]]
        assert refined
        assert any(f["rewritten_sql"] and "CLEAN_ID" in f["rewritten_sql"] for f in refined)

        res_proj = analyze_project_wide(db, proj.id, run_llm=True, llm_client=fake_llm)
        assert res_proj["ai_status"] == "COMPLETED"
        assert res_proj["ai_roadmap"].get("executive_summary")

        # Verify BLOCK floor protects against rogue downgrade model and Scheduler logs BUS_REJECTED
        rogue_llm = _ScriptedFakeLlm(attempt_downgrade=True)
        rev = evaluate_script_change(
            db,
            proj.id,
            change_type="MODIFY",
            filename="SP_DEMO_OPT.sql",
            target_script_id=script.id,
            new_content="CREATE OR REPLACE PROCEDURE SP_DEMO_OPT IS\nBEGIN\n  DELETE FROM TGT_TBL;\nEND;",
            run_llm=True,
            llm_client=rogue_llm,
        )
        assert rev["deterministic_verdict"] == "BLOCK"
        assert rev["final_verdict"] == "BLOCK"

        from app.infra.db_tables import EventModel, RunModel
        from app.policy import project

        review_events = db.query(EventModel).filter(EventModel.run_id == rev["run_id"]).all()
        assert any(e.kind == "BUS_REJECTED" for e in review_events)
        assert project(db, db.get(RunModel, res_single["run_id"])).status == "COMPLETED"
        assert project(db, db.get(RunModel, res_proj["run_id"])).status == "COMPLETED"


def test_scheduler_bsp_multi_chunk_and_offline_project_run_completion(tmp_path: Path):
    from app.infra.db_tables import EventModel, RunModel
    from app.policy import project

    Session = _new_test_session(tmp_path)
    with Session() as db:
        multi_sql = """CREATE OR REPLACE PROCEDURE SP_MULTI_OPT IS
BEGIN
  INSERT INTO TGT_A (ID) SELECT A.ID FROM SRC_A A INNER JOIN SRC_B B ON TRIM(A.ID) = B.ID;
  INSERT INTO TGT_B SELECT * FROM SRC_C WHERE NAME LIKE '%TEST%';
  UPDATE TGT_A SET FLAG = '1';
  COMMIT;
END;"""
        proj = ingest_collected_scripts(
            db,
            name="BSP Multi Chunk",
            source_mode="LOCAL_DIR",
            source_path="/tmp/bsp",
            dialect="oracle",
            collected=[CollectedScript("SP_MULTI_OPT.sql", "SP_MULTI_OPT.sql", multi_sql, "multi123")],
        )
        script = db.query(ScriptFileModel).filter(ScriptFileModel.project_id == proj.id).first()

        fake_llm = _ScriptedFakeLlm(attempt_downgrade=False, priority_limit=3)
        res_single = analyze_single_script(
            db,
            proj.id,
            script.id,
            run_llm=True,
            llm_client=fake_llm,
            max_chunks_to_refine=3,
        )
        assert res_single["ai_status"] == "COMPLETED"
        assert res_single["ai_notes"]["analyzed_chunks"] >= 2
        single_run = db.get(RunModel, res_single["run_id"])
        assert project(db, single_run).status == "COMPLETED"
        single_kinds = [
            e.kind for e in db.query(EventModel).filter(EventModel.run_id == single_run.id).order_by(EventModel.seq.asc()).all()
        ]
        assert "STATIC_SCANNED" in single_kinds
        assert "CHUNK_ANALYZED" in single_kinds
        assert single_kinds[-1] == "ANALYSIS_COMPLETED"

        res_proj_offline = analyze_project_wide(db, proj.id, run_llm=False)
        assert res_proj_offline["ai_status"] == "SKIPPED"
        proj_run = db.get(RunModel, res_proj_offline["run_id"])
        assert project(db, proj_run).status == "COMPLETED"


def test_fastapi_4_domain_endpoints_e2e(tmp_path: Path):
    Session = _new_test_session(tmp_path)
    repo_dir = tmp_path / "sql_repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "SP_ETL_A.sql").write_text(
        """CREATE OR REPLACE PROCEDURE SP_ETL_A IS
BEGIN
  INSERT INTO DWD_RES (ID) SELECT ID FROM ODS_SRC WHERE CODE LIKE '%ABC%';
  COMMIT;
EXCEPTION WHEN OTHERS THEN ROLLBACK; RAISE;
END;""",
        encoding="utf-8",
    )

    def _override_get_db():
        with Session() as s:
            yield s

    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as client:
            assert client.get("/api/system/health").status_code == 200
            assert client.get("/api/settings").status_code == 200

            imp = client.post(
                "/api/projects/import-dir",
                json={"name": "E2E Repo", "dir_path": str(repo_dir), "dialect": "oracle"},
            )
            assert imp.status_code == 200, imp.text
            proj_data = imp.json()
            pid = proj_data["id"]
            assert proj_data["script_count"] == 1

            an = client.post(f"/api/projects/{pid}/analyze", json={"run_llm": False})
            assert an.status_code == 200
            assert an.json()["script_findings"] >= 1

            flist = client.get(f"/api/projects/{pid}/findings").json()["findings"]
            assert len(flist) >= 1
            fid = flist[0]["id"]
            assert client.patch(f"/api/findings/{fid}", json={"status": "ADOPTED"}).json()["status"] == "ADOPTED"

            rev_resp = client.post(
                f"/api/projects/{pid}/reviews",
                json={
                    "change_type": "ADD",
                    "filename": "SP_ETL_B.sql",
                    "new_content": "CREATE OR REPLACE PROCEDURE SP_ETL_B IS\nBEGIN\n  INSERT INTO DM_RES (ID) SELECT ID FROM DWD_RES;\nEXCEPTION WHEN OTHERS THEN ROLLBACK; RAISE;\nEND;",
                    "author": "e2e_user",
                    "run_llm": False,
                },
            )
            assert rev_resp.status_code == 200
            rev_id = rev_resp.json()["id"]
            run_id = rev_resp.json()["run_id"]

            app_resp = client.post(f"/api/reviews/{rev_id}/approve", json={"reviewer": "lead", "merge_baseline": True})
            assert app_resp.status_code == 200
            assert app_resp.json()["merged_to_baseline"] is True

            md_resp = client.get(f"/api/reviews/{rev_id}/export?format=json")
            assert md_resp.status_code == 200
            assert "SP_ETL_B.sql" in md_resp.json()["markdown"]

            tr_resp = client.get(f"/api/runs/{run_id}/trace")
            assert tr_resp.status_code == 200
            assert tr_resp.json()["chain_verified"] is True
    finally:
        app.dependency_overrides.clear()
