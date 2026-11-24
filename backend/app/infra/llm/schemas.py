"""受控大模型 6 个 Tool-Call JSON Schema 契约（基础设施层 Layer 0）。

聚焦面向数据研发与 DBA 的专业 SQL 工程诊断、可验证 SQL 改写、全项目重构路线图与上线前变更门禁评估：
1. `plan_script_review`：针对大脚本切块清单确定高优先级审计切块；
2. `diagnose_chunk_issues`：输出单个切块的根因剖析、误报澄清与优化策略；
3. `propose_sql_rewrite`：产出可直接落地的等价优化 SQL（受 `sqlglot` 语法门禁校验）；
4. `synthesize_project_optimizations`：提炼跨脚本合并、共享表解耦与全仓 P0~P3 重构路线图；
5. `assess_change_risk`：针对新增/修改脚本产出回归风险、验证比对 SQL、回刷清单与回滚方案；
6. `summarize_release_gate`：输出研发/DBA/测试三视角上线门禁结论（受 BLOCK 不可降级门禁约束）。
"""

from typing import Any, Dict

TOOL_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "plan_script_review": {
        "type": "object",
        "properties": {
            "script_purpose": {"type": "string"},
            "priority_chunk_ids": {"type": "array", "items": {"type": "string"}},
            "review_focus": {"type": "string"},
        },
        "required": ["script_purpose", "priority_chunk_ids", "review_focus"],
    },
    "diagnose_chunk_issues": {
        "type": "object",
        "properties": {
            "chunk_id": {"type": "string"},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "rule_code": {"type": "string"},
                        "severity": {
                            "type": "string",
                            "enum": ["P0_BLOCKER", "P1_HIGH", "P2_MEDIUM", "P3_LOW"],
                        },
                        "category": {
                            "type": "string",
                            "enum": [
                                "CORRECTNESS",
                                "PERFORMANCE",
                                "IDEMPOTENCY",
                                "TRANSACTION",
                                "MAINTAINABILITY",
                                "ARCHITECTURE",
                            ],
                        },
                        "root_cause": {"type": "string"},
                        "actionable_fix": {"type": "string"},
                        "expected_benefit": {"type": "string"},
                        "false_positive": {"type": "boolean"},
                    },
                    "required": ["rule_code", "severity", "category", "root_cause", "actionable_fix"],
                },
            },
        },
        "required": ["chunk_id", "findings"],
    },
    "propose_sql_rewrite": {
        "type": "object",
        "properties": {
            "chunk_id": {"type": "string"},
            "rule_code": {"type": "string"},
            "optimization_summary": {"type": "string"},
            "rewritten_sql": {"type": "string"},
            "expected_benefit": {"type": "string"},
            "equivalence_notes": {"type": "string"},
        },
        "required": ["chunk_id", "optimization_summary", "rewritten_sql", "expected_benefit"],
    },
    "synthesize_project_optimizations": {
        "type": "object",
        "properties": {
            "executive_summary": {"type": "string"},
            "architecture_recommendations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "priority": {"type": "string", "enum": ["P0_BLOCKER", "P1_HIGH", "P2_MEDIUM", "P3_LOW"]},
                        "category": {
                            "type": "string",
                            "enum": ["ARCHITECTURE", "PERFORMANCE", "IDEMPOTENCY", "TRANSACTION", "CORRECTNESS"],
                        },
                        "title": {"type": "string"},
                        "affected_tables": {"type": "array", "items": {"type": "string"}},
                        "affected_scripts": {"type": "array", "items": {"type": "string"}},
                        "proposal": {"type": "string"},
                        "expected_benefit": {"type": "string"},
                    },
                    "required": ["priority", "category", "title", "proposal", "expected_benefit"],
                },
            },
        },
        "required": ["executive_summary", "architecture_recommendations"],
    },
    "assess_change_risk": {
        "type": "object",
        "properties": {
            "risk_summary": {"type": "string"},
            "semantic_risks": {"type": "array", "items": {"type": "string"}},
            "verification_sqls": {"type": "array", "items": {"type": "string"}},
            "backfill_scripts": {"type": "array", "items": {"type": "string"}},
            "rollback_plan": {"type": "string"},
        },
        "required": ["risk_summary", "semantic_risks", "verification_sqls", "rollback_plan"],
    },
    "summarize_release_gate": {
        "type": "object",
        "properties": {
            "recommended_verdict": {"type": "string", "enum": ["PASS", "WARN", "BLOCK"]},
            "developer_notes": {"type": "string"},
            "dba_notes": {"type": "string"},
            "qa_notes": {"type": "string"},
            "release_checklist": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["recommended_verdict", "developer_notes", "dba_notes", "qa_notes", "release_checklist"],
    },
}


def tool_prompt_preamble(tool_name: str) -> str:
    return (
        f"请严格通过 `{tool_name}` 返回符合 JSON Schema 的结构化工程评估参数。"
        "严禁编造不存在的表名、字段名、切块编号或脚本名称；如产出 SQL 改写，必须保持语义等价且通过对应数据库方言语法解析。"
    )
