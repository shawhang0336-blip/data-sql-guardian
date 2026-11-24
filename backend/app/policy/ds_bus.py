"""ds_agent 五大确定性事前门禁裁决器（策略层 Layer 2）。

1. `chunk_id_whitelist_adjudicator`：拦截虚构 `chunk_id` 与 `priority_chunk_ids`；
2. `entity_whitelist_adjudicator`：拦截 AI 在项目建议或改写中捏造不存在的表名；
3. `severity_category_adjudicator`：强制严重级别与缺陷分类枚举合法；
4. `sql_rewrite_syntax_adjudicator`：使用 `sqlglot` 对 `rewritten_sql` 执行多方言语法预检，
   禁止语法损坏、擅自 `DROP` 或丢失原有核心目标写入表；
5. `release_gate_floor_adjudicator`：上线评估硬红线——确定性门禁已判 `BLOCK` 时，
   严禁 AI 将 `recommended_verdict` 降级为 `PASS` 或 `WARN`。
"""

import re
from typing import Any, Dict, List, Optional, Set

import sqlglot
from sqlglot import exp

from app.kernel.bus import Bus
from app.kernel.run import BudgetGuard, BudgetLimits

_VALID_SEVERITIES = {"P0_BLOCKER", "P1_HIGH", "P2_MEDIUM", "P3_LOW"}
_VALID_CATEGORIES = {"CORRECTNESS", "PERFORMANCE", "IDEMPOTENCY", "TRANSACTION", "MAINTAINABILITY", "ARCHITECTURE"}
_VERDICT_RANK = {"PASS": 0, "WARN": 1, "BLOCK": 2}
_DIALECTS = ("oracle", "postgres", "mysql", "tsql", "spark", None)


def _norm_ident(name: str) -> str:
    txt = str(name or "").replace('"', "").replace("`", "").strip().upper()
    return txt.rsplit(".", 1)[-1].strip() if "." in txt else txt


def chunk_id_whitelist_adjudicator(
    tool_name: str, args: Dict[str, Any], payload: Dict[str, Any]
) -> Optional[str]:
    if not isinstance(args, dict) or not isinstance(payload, dict):
        return None
    allowed = payload.get("allowed_chunk_ids")
    if not isinstance(allowed, list):
        return None
    allowed_set = set(str(x) for x in allowed)
    if "chunk_id" in args and str(args["chunk_id"]) not in allowed_set:
        return f"切块幻觉拦截：chunk_id `{args.get('chunk_id')}` 不在当前脚本合法切块清单中"
    if "priority_chunk_ids" in args and isinstance(args["priority_chunk_ids"], list):
        for cid in args["priority_chunk_ids"]:
            if str(cid) not in allowed_set:
                return f"切块幻觉拦截：priority_chunk_ids 中的 `{cid}` 不在合法切块清单中"
    return None


def entity_whitelist_adjudicator(
    tool_name: str, args: Dict[str, Any], payload: Dict[str, Any]
) -> Optional[str]:
    if not isinstance(args, dict) or not isinstance(payload, dict):
        return None
    allowed_tables = payload.get("allowed_tables")
    if not isinstance(allowed_tables, list) or not allowed_tables:
        return None
    allowed_norm = {_norm_ident(t) for t in allowed_tables if t}
    if tool_name == "synthesize_project_optimizations":
        for rec in args.get("architecture_recommendations") or []:
            if not isinstance(rec, dict):
                continue
            for tbl in rec.get("affected_tables") or []:
                norm = _norm_ident(tbl)
                if norm and norm not in allowed_norm:
                    return f"实体幻觉拦截：架构建议中引用的表 `{tbl}` 不在当前项目真实表清单中"
    return None


def severity_category_adjudicator(
    tool_name: str, args: Dict[str, Any], payload: Dict[str, Any]
) -> Optional[str]:
    if not isinstance(args, dict):
        return None
    if tool_name == "diagnose_chunk_issues":
        for item in args.get("findings") or []:
            if not isinstance(item, dict):
                continue
            sev = str(item.get("severity") or "")
            cat = str(item.get("category") or "")
            if sev and sev not in _VALID_SEVERITIES:
                return f"严重级别枚举非法：`{sev}`，允许值：{sorted(_VALID_SEVERITIES)}"
            if cat and cat not in _VALID_CATEGORIES:
                return f"问题分类枚举非法：`{cat}`，允许值：{sorted(_VALID_CATEGORIES)}"
    if tool_name == "synthesize_project_optimizations":
        for rec in args.get("architecture_recommendations") or []:
            if not isinstance(rec, dict):
                continue
            prio = str(rec.get("priority") or "")
            if prio and prio not in _VALID_SEVERITIES:
                return f"重构优先级枚举非法：`{prio}`"
    return None


def _strip_plsql_comments(sql_text: str) -> str:
    s = re.sub(r"/\*.*?\*/", " ", sql_text or "", flags=re.DOTALL)
    s = re.sub(r"--[^\n]*", " ", s).strip()
    return s


def _split_top_level_statements(sql_text: str) -> List[str]:
    stmts: List[str] = []
    buf: List[str] = []
    in_sq = False
    for ch in _strip_plsql_comments(sql_text):
        if ch == "'":
            in_sq = not in_sq
            buf.append(ch)
        elif ch == ";" and not in_sq:
            part = "".join(buf).strip()
            if part:
                stmts.append(part)
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        stmts.append(tail)
    return stmts


def _parse_any_dialect(stmt: str) -> Optional[exp.Expression]:
    for dialect in _DIALECTS:
        try:
            tree = sqlglot.parse_one(stmt, read=dialect)
            if tree is not None and not isinstance(tree, exp.Command):
                return tree
        except Exception:  # noqa: BLE001
            continue
    return None


def sql_rewrite_syntax_adjudicator(
    tool_name: str, args: Dict[str, Any], payload: Dict[str, Any]
) -> Optional[str]:
    if tool_name != "propose_sql_rewrite" or not isinstance(args, dict):
        return None
    rewritten = str(args.get("rewritten_sql") or "").strip()
    if not rewritten:
        return "改写 SQL 不能为空，若无需改写请保留等价优化后的完整 SQL 语句"
    if re.search(r"\bDROP\s+(?:TABLE|DATABASE|SCHEMA)\b", rewritten, flags=re.IGNORECASE):
        return "安全拦截：优化改写 SQL 中严禁引入 DROP TABLE / DATABASE 等破坏性操作"

    stmts = _split_top_level_statements(rewritten)
    if not stmts:
        return "改写 SQL 未包含可执行语句"

    parsed_trees: List[exp.Expression] = []
    for st in stmts:
        up = st.upper().strip()
        if up in {"COMMIT", "ROLLBACK"}:
            continue
        tree = _parse_any_dialect(st)
        if tree is None:
            return f"改写 SQL 语法门禁拦截：语句无法通过 sqlglot 多方言解析 -> `{st[:120]}`"
        parsed_trees.append(tree)

    if not parsed_trees:
        return "改写 SQL 缺少实质查询或 DML 语句"

    required_targets = payload.get("required_target_tables") if isinstance(payload, dict) else None
    if isinstance(required_targets, list) and required_targets:
        found_targets: Set[str] = set()
        for tree in parsed_trees:
            if isinstance(tree, (exp.Insert, exp.Merge, exp.Update, exp.Create)):
                tbl = tree.find(exp.Table)
                if tbl is not None:
                    found_targets.add(_norm_ident(tbl.name))
        missing = [_norm_ident(t) for t in required_targets if _norm_ident(t) not in found_targets]
        if missing:
            return f"目标表语义丢失拦截：改写 SQL 必须保留对原目标表 `{missing}` 的写入"
    return None


def release_gate_floor_adjudicator(
    tool_name: str, args: Dict[str, Any], payload: Dict[str, Any]
) -> Optional[str]:
    if tool_name != "summarize_release_gate" or not isinstance(args, dict) or not isinstance(payload, dict):
        return None
    det_verdict = str(payload.get("deterministic_verdict") or "PASS").upper()
    ai_verdict = str(args.get("recommended_verdict") or "").upper()
    if ai_verdict not in _VERDICT_RANK:
        return f"上线准入推荐结论非法：`{ai_verdict}`（仅允许 PASS / WARN / BLOCK）"
    if _VERDICT_RANK[ai_verdict] < _VERDICT_RANK.get(det_verdict, 0):
        return (
            f"上线门禁硬红线拦截：确定性静态/影响面门禁已裁决 `{det_verdict}`，"
            f"模型严禁擅自降级为 `{ai_verdict}`！"
        )
    return None


def default_bus(parent: Optional[Bus] = None) -> Bus:
    """装配 ds_agent 独立总线（挂载全部 5 道确定性事前门禁，零全局污染）。"""
    bus = Bus(parent=parent)
    bus.add_adjudicator(chunk_id_whitelist_adjudicator)
    bus.add_adjudicator(entity_whitelist_adjudicator)
    bus.add_adjudicator(severity_category_adjudicator)
    bus.add_adjudicator(sql_rewrite_syntax_adjudicator)
    bus.add_adjudicator(release_gate_floor_adjudicator)
    return bus


def build_run_bus(
    budget: Optional[Dict[str, Any]] = None,
    *,
    parent: Optional[Bus] = None,
    run_id: Optional[str] = None,
) -> Bus:
    bus = default_bus(parent=parent)
    if budget:
        guard = BudgetGuard(BudgetLimits.from_dict(budget), run_id=run_id)
        guard.attach(bus)
    return bus


def build_script_child_bus(
    max_calls: int = 8,
    *,
    max_elapsed_seconds: int = 300,
    parent: Optional[Bus] = None,
    script_tag: Optional[str] = None,
) -> Bus:
    bus = default_bus(parent=parent)
    limits = BudgetLimits(
        max_llm_calls=max(1, int(max_calls)) if max_calls and int(max_calls) > 0 else None,
        max_total_tokens=None,
        max_elapsed_seconds=float(max_elapsed_seconds) if max_elapsed_seconds > 0 else None,
    )
    guard = BudgetGuard(limits, run_id=script_tag)
    guard.attach(bus)
    return bus
