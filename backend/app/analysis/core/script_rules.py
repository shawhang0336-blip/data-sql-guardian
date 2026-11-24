"""16 deterministic single-script SQL / PL/SQL static audit & optimization rules (pure offline)."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional


SINGLE_SCRIPT_RULE_CATALOG: Dict[str, Dict[str, str]] = {
    "S001": {"severity": "P0", "category": "CORRECTNESS", "title": "NULL 条件与等值子查询/连接矛盾（空值逻辑死锁）"},
    "S002": {"severity": "P0", "category": "CORRECTNESS", "title": "UPDATE / DELETE 缺少 WHERE 条件（全表误改/误删风险）"},
    "S003": {"severity": "P0", "category": "CORRECTNESS", "title": "NOT IN 语句存在 NULL 逻辑陷阱"},
    "S004": {"severity": "P0", "category": "RELIABILITY", "title": "存储过程包含 DML/DDL 但缺少 EXCEPTION 异常处理"},
    "S005": {"severity": "P0", "category": "RELIABILITY", "title": "TRUNCATE 与多点 COMMIT 混用（中断后无回滚幂等保障）"},
    "S006": {"severity": "P1", "category": "PERFORMANCE", "title": "JOIN 连接键包裹函数（索引失效 / Hash 倾斜）"},
    "S007": {"severity": "P1", "category": "PERFORMANCE", "title": "WHERE 过滤列包裹函数（分区裁剪与索引失效）"},
    "S008": {"severity": "P1", "category": "PERFORMANCE", "title": "前导通配符 LIKE '%...' 触发全表扫描"},
    "S009": {"severity": "P1", "category": "CORRECTNESS", "title": "INSERT INTO 未显式声明目标列名清单"},
    "S010": {"severity": "P1", "category": "RELIABILITY", "title": "批处理写入使用非确定性主键函数（SYS_GUID/UUID/RANDOM）"},
    "S011": {"severity": "P1", "category": "PERFORMANCE", "title": "隐式/显式笛卡尔积连接（CROSS JOIN 或无约束多表 JOIN）"},
    "S012": {"severity": "P1", "category": "SECURITY", "title": "动态 SQL（EXECUTE IMMEDIATE）存在注入或血缘不可见风险"},
    "S013": {"severity": "P1", "category": "MAINTAINABILITY", "title": "单脚本内多次重复写入/扫描同表（可合并为单次扫描与分支插入）"},
    "S014": {"severity": "P2", "category": "PERFORMANCE", "title": "使用 SELECT * 导致宽表冗余 IO 与契约脆弱"},
    "S015": {"severity": "P2", "category": "RELIABILITY", "title": "脚本包含运行时系统时间分支（SYSDATE / CURRENT_TIMESTAMP）破坏重跑确定性"},
    "S016": {"severity": "P2", "category": "PERFORMANCE", "title": "滥用 SELECT DISTINCT / 非 ALL UNION 或跨列 OR 增加排序开销"},
}


def _make_finding(
    *,
    script_id: str,
    chunk: Optional[Dict[str, Any]],
    rule_code: str,
    description: str,
    evidence_snippet: str,
    suggestion: str,
    expected_benefit: str,
    related_tables: Optional[List[str]] = None,
    severity_override: Optional[str] = None,
) -> Dict[str, Any]:
    meta = SINGLE_SCRIPT_RULE_CATALOG[rule_code]
    start_line = int(chunk["start_line"]) if chunk else 1
    end_line = int(chunk["end_line"]) if chunk else 1
    tables = related_tables
    if tables is None and chunk is not None:
        tables = list(dict.fromkeys((chunk.get("target_tables") or []) + (chunk.get("source_tables") or [])))
    return {
        "scope": "SCRIPT",
        "script_id": script_id,
        "chunk_id": chunk.get("id") if chunk else None,
        "rule_code": rule_code,
        "category": meta["category"],
        "severity": severity_override or meta["severity"],
        "title": meta["title"],
        "description": description,
        "evidence_snippet": evidence_snippet[:500],
        "start_line": start_line,
        "end_line": end_line,
        "related_tables": tables or [],
        "related_scripts": [script_id],
        "suggestion": suggestion,
        "rewritten_sql": None,
        "expected_benefit": expected_benefit,
    }


def _excerpt(raw_sql: str, max_chars: int = 360) -> str:
    lines = [ln.rstrip() for ln in raw_sql.strip().splitlines() if ln.strip()]
    joined = "\n".join(lines[:10])
    return joined[:max_chars]


def evaluate_script_rules(
    script: Dict[str, Any],
    chunks: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Run all 16 deterministic single-script rules against parsed script metadata & chunks."""
    script_id = str(script["id"])
    file_kind = str(script.get("file_kind", "SCRIPT")).upper()
    commit_count = int(script.get("commit_count", 0))
    has_exception = bool(script.get("has_exception_handler", False))
    findings: List[Dict[str, Any]] = []

    has_dml_or_ddl = False
    truncate_chunks: List[Dict[str, Any]] = []
    target_write_chunks: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    for ch in chunks:
        feats = ch.get("ast_features") or {}
        kind = str(ch.get("statement_kind", "OTHER")).upper()
        raw_sql = str(ch.get("raw_sql", ""))
        ctx = str(ch.get("control_context", ""))

        if kind in ("INSERT", "MERGE", "UPDATE", "DELETE", "TRUNCATE", "CTAS", "CREATE_TABLE"):
            has_dml_or_ddl = True
        if kind == "TRUNCATE" or feats.get("has_truncate"):
            truncate_chunks.append(ch)
        if kind in ("INSERT", "MERGE", "UPDATE"):
            for tgt in ch.get("target_tables") or []:
                target_write_chunks[tgt].append(ch)

        # S001: NULL predicate dead join/NOT EXISTS correlation
        dead_cols = feats.get("dead_null_join_columns") or []
        if dead_cols:
            findings.append(
                _make_finding(
                    script_id=script_id,
                    chunk=ch,
                    rule_code="S001",
                    description=(
                        f"语句第 {ch['start_line']}-{ch['end_line']} 行对列 {', '.join(dead_cols)} "
                        f"同时施加 `IS NULL` 过滤并在关联/NOT EXISTS 子查询中执行 `=` 等值比较。"
                        "SQL 三值逻辑下 `= NULL` 恒为 UNKNOWN（非 TRUE），导致子查询判空永远不成立或连接失效。"
                    ),
                    evidence_snippet=_excerpt(raw_sql),
                    suggestion=(
                        f"将关联谓词改写为 `NVL({dead_cols[0]}, '#NULL#') = NVL(...)` 或使用 `IS NOT DISTINCT FROM`，"
                        "或确认业务是否意在判断 `IS NOT NULL` / 基于其他主键列关联。"
                    ),
                    expected_benefit="消除静默错数与重复插入 Bug，保证账实核对结果正确性。",
                )
            )

        # S002: UPDATE / DELETE without WHERE
        if feats.get("has_missing_where_mutation"):
            findings.append(
                _make_finding(
                    script_id=script_id,
                    chunk=ch,
                    rule_code="S002",
                    description=f"语句第 {ch['start_line']}-{ch['end_line']} 行执行 `{kind}` 但缺少 `WHERE` 过滤条件，将改写/清空整张表。",
                    evidence_snippet=_excerpt(raw_sql),
                    suggestion="补充明确的主键或分区过滤条件；若确需全表清理，请改用 `TRUNCATE TABLE` 并记录变更。",
                    expected_benefit="避免线上批量任务误改/误删全表数据。",
                )
            )

        # S003: NOT IN NULL trap
        if feats.get("has_not_in"):
            is_subq = bool(feats.get("not_in_subquery"))
            findings.append(
                _make_finding(
                    script_id=script_id,
                    chunk=ch,
                    rule_code="S003",
                    severity_override="P0" if is_subq else "P1",
                    description=(
                        f"语句第 {ch['start_line']}-{ch['end_line']} 行使用了 "
                        f"{'`NOT IN (SELECT ...)` 子查询' if is_subq else '`NOT IN (...)` 排除列表'}。"
                        "当子查询或对比列存在 NULL 时，`NOT IN` 可能返回空集或遗漏空值记录，且通常无法利用索引。"
                    ),
                    evidence_snippet=_excerpt(raw_sql),
                    suggestion="优先改写为 `NOT EXISTS (SELECT 1 FROM ...)` 并显式处理 NULL（如 `NVL(col, '-')`）。",
                    expected_benefit="杜绝 NULL 值导致的静默漏数，并将反连接优化为 HASH ANTI JOIN。",
                )
            )

        # S006: Function wrapped column in JOIN ON
        bad_joins = [
            jc for jc in (feats.get("join_conditions") or [])
            if jc.get("left_func_wrapped") or jc.get("right_func_wrapped")
        ]
        if bad_joins:
            sample = "; ".join(jc.get("raw", "") for jc in bad_joins[:3])
            findings.append(
                _make_finding(
                    script_id=script_id,
                    chunk=ch,
                    rule_code="S006",
                    description=(
                        f"第 {ch['start_line']}-{ch['end_line']} 行 JOIN 连接条件对连接列包裹了函数（如 `{sample}`），"
                        "导致表索引失效且统计信息无法准确估算基数，易触发低效全表扫描与数据倾斜。"
                    ),
                    evidence_snippet=sample or _excerpt(raw_sql),
                    suggestion="在落入 ODS/Staging 阶段提前清洗归一化连接键列，或建立函数索引，使 JOIN 使用裸列等值连接。",
                    expected_benefit="恢复索引/分区裁剪及优化器基数估计，预计降低 JOIN 耗时 40%~80%。",
                )
            )

        # S007: Function wrapped column in WHERE
        where_funcs = feats.get("where_func_columns") or []
        if where_funcs:
            findings.append(
                _make_finding(
                    script_id=script_id,
                    chunk=ch,
                    rule_code="S007",
                    description=(
                        f"第 {ch['start_line']}-{ch['end_line']} 行 WHERE 条件对列 {', '.join(where_funcs)} 使用函数转换，"
                        "导致常规 B-Tree 索引及日期分区裁剪（Partition Pruning）失效。"
                    ),
                    evidence_snippet=_excerpt(raw_sql),
                    suggestion="将函数计算移至常量/参数侧（如 `DT >= TO_DATE(:V_DATE, 'YYYYMMDD') AND DT < TO_DATE(:V_DATE, 'YYYYMMDD') + 1`）。",
                    expected_benefit="恢复索引范围扫描与分区裁剪，大幅降低冷数据扫描 IO。",
                )
            )

        # S008: Leading wildcard LIKE '%...'
        likes = feats.get("leading_wildcard_likes") or []
        if likes:
            findings.append(
                _make_finding(
                    script_id=script_id,
                    chunk=ch,
                    rule_code="S008",
                    description=f"第 {ch['start_line']}-{ch['end_line']} 行存在前导通配符模糊匹配：{', '.join(likes)}，无法使用普通前缀索引。",
                    evidence_snippet=", ".join(likes),
                    suggestion="若为枚举分类筛选，改为标准码值列等值/`IN` 过滤或建立反向/全文索引。",
                    expected_benefit="避免大表字符模糊匹配引发的全表 CPU/IO 飙升。",
                )
            )

        # S009: INSERT without column list
        if feats.get("has_insert_without_columns"):
            findings.append(
                _make_finding(
                    script_id=script_id,
                    chunk=ch,
                    rule_code="S009",
                    description=f"第 {ch['start_line']}-{ch['end_line']} 行 `INSERT INTO` 未显式指定目标列名列表，高度依赖表字段物理顺序。",
                    evidence_snippet=_excerpt(raw_sql),
                    suggestion="显式列出 `INSERT INTO TABLE_NAME (COL1, COL2, ...) SELECT ...`。",
                    expected_benefit="消除后续加列 DDL 引发的错位写入或任务中断风险。",
                )
            )

        # S010: Non-deterministic key in batch DML
        if feats.get("has_nondeterministic_key"):
            findings.append(
                _make_finding(
                    script_id=script_id,
                    chunk=ch,
                    rule_code="S010",
                    description=f"第 {ch['start_line']}-{ch['end_line']} 行批处理写入使用 `SYS_GUID()` / `UUID()` / `RANDOM()` 生成主键，重跑时主键值变化，破坏幂等性与核对可追溯性。",
                    evidence_snippet=_excerpt(raw_sql),
                    suggestion="改为基于业务自然键 + 账期生成的确定性哈希主键（或标准序列）。",
                    expected_benefit="实现重跑幂等，便于增量补数与上下游对账。",
                )
            )

        # S011: Implicit cross join
        if feats.get("has_implicit_cross_join"):
            findings.append(
                _make_finding(
                    script_id=script_id,
                    chunk=ch,
                    rule_code="S011",
                    description=f"第 {ch['start_line']}-{ch['end_line']} 行疑似隐式/显式笛卡尔积（`CROSS JOIN` 或逗号多表缺少有效关联谓词），存在行集膨胀风险。",
                    evidence_snippet=_excerpt(raw_sql),
                    suggestion="统一改用标准 ANSI `INNER JOIN ... ON ...` 并确认关联键唯一性。",
                    expected_benefit="防止记录数爆炸和临时表空间耗尽。",
                )
            )

        # S012: Dynamic SQL risk
        if ch.get("is_dynamic_sql"):
            findings.append(
                _make_finding(
                    script_id=script_id,
                    chunk=ch,
                    rule_code="S012",
                    description=f"第 {ch['start_line']}-{ch['end_line']} 行使用了 `EXECUTE IMMEDIATE` 动态拼接 SQL，静态血缘穿透与参数化校验难度高。",
                    evidence_snippet=_excerpt(raw_sql),
                    suggestion="对于动态表名/条件优先采用绑定变量 (`USING`) 或白名单校验；能静态化的 DML 尽量拆分为静态 SQL。",
                    expected_benefit="降低 SQL 注入风险并提升表级血缘分析与编译期校验覆盖率。",
                )
            )

        # S014: SELECT *
        if feats.get("has_select_star"):
            findings.append(
                _make_finding(
                    script_id=script_id,
                    chunk=ch,
                    rule_code="S014",
                    description=f"第 {ch['start_line']}-{ch['end_line']} 行存在 `SELECT *` 全列读取，增加无效网络/磁盘吞吐并易受上游加列影响。",
                    evidence_snippet=_excerpt(raw_sql),
                    suggestion="将 `*` 替换为实际所需业务字段清单。",
                    expected_benefit="减少列存/行存读取宽表开销并加固接口契约。",
                )
            )

        # S015: SYSDATE / CURRENT_TIMESTAMP branch in statement or IF control_context
        has_time_ctx = any(tok in ctx.upper() for tok in ("SYSDATE", "CURRENT_TIMESTAMP", "SYSTIMESTAMP"))
        if feats.get("has_sysdate_branch") or has_time_ctx:
            findings.append(
                _make_finding(
                    script_id=script_id,
                    chunk=ch,
                    rule_code="S015",
                    description=(
                        f"第 {ch['start_line']}-{ch['end_line']} 行依赖系统当前时间（上下文：`{ctx}`），"
                        "不同时间窗执行（如凌晨批量与日间补数重跑）将走不同分支或产出不同结果。"
                    ),
                    evidence_snippet=f"[{ctx}] " + _excerpt(raw_sql, 240),
                    suggestion="将业务基准日/时间窗参数（如 `V_BIZ_DATE`、`V_CUTOFF_HOUR`）显式入参，避免隐式依赖 `SYSDATE`。",
                    expected_benefit="保证批处理补数重跑与历史回溯结果严格一致。",
                )
            )

        # S016: DISTINCT / UNION without ALL / OR across columns
        if feats.get("has_distinct") or feats.get("has_union_without_all") or feats.get("has_or_across_columns"):
            reasons = []
            if feats.get("has_distinct"):
                reasons.append("SELECT DISTINCT 全局去重排序")
            if feats.get("has_union_without_all"):
                reasons.append("UNION 触发全集排序去重（可评估改为 UNION ALL）")
            if feats.get("has_or_across_columns"):
                reasons.append("跨列 OR 导致索引合并受限")
            findings.append(
                _make_finding(
                    script_id=script_id,
                    chunk=ch,
                    rule_code="S016",
                    description=f"第 {ch['start_line']}-{ch['end_line']} 行包含性能敏感算子：{'；'.join(reasons)}。",
                    evidence_snippet=_excerpt(raw_sql),
                    suggestion="明确数据集无重复时使用 `UNION ALL`；用精确 `EXISTS`/窗口去重替代盲目 `DISTINCT`；跨列 `OR` 可评估拆为 `UNION ALL`。",
                    expected_benefit="消除不必要的 Sort/Unique 算子，降低 PGA/Temp 空间压力。",
                )
            )

    # Script-level rule S004: Procedure with DML/DDL missing EXCEPTION handler
    if file_kind == "PROCEDURE" and has_dml_or_ddl and not has_exception:
        first_chunk = chunks[0] if chunks else None
        findings.append(
            _make_finding(
                script_id=script_id,
                chunk=first_chunk,
                rule_code="S004",
                description=(
                    f"存储过程 `{script['filename']}` 执行了数据写入/清理操作，"
                    "但全文未定义 `EXCEPTION` 异常捕获与 `ROLLBACK`/错误日志记录块。"
                ),
                evidence_snippet=f"PROCEDURE {script['filename']} (missing EXCEPTION block)",
                suggestion="在过程末尾补充 `EXCEPTION WHEN OTHERS THEN ROLLBACK; 记录错误表; RAISE;`。",
                expected_benefit="确保异常中断时事务完整回滚并输出精准排障上下文。",
                related_tables=list(script.get("target_tables") or []),
            )
        )

    # Script-level rule S005: TRUNCATE mixed with multiple COMMITs
    if truncate_chunks and commit_count >= 2:
        tr_chunk = truncate_chunks[0]
        findings.append(
            _make_finding(
                script_id=script_id,
                chunk=tr_chunk,
                rule_code="S005",
                description=(
                    f"脚本 `{script['filename']}` 包含 {len(truncate_chunks)} 处 `TRUNCATE` 及 "
                    f"{commit_count} 处分段 `COMMIT`。"
                    "Oracle 中 TRUNCATE 触发隐式提交且无法回滚，若后续步骤失败将导致目标表长时间处于清空半成品状态。"
                ),
                evidence_snippet=_excerpt(tr_chunk["raw_sql"]),
                suggestion="改用临时 Staging 表完成计算后再原子交换/MERGE，或采用按账期分区清理（`DELETE WHERE BIZ_DATE = ...` + 单事务提交）。",
                expected_benefit="大幅提高失败重跑安全性，消除下游读取空表的窗口期。",
            )
        )

    # Script-level rule S013: Intra-script repeated writes to the same table (>=3 DML chunks or >=2 repeated source/target patterns)
    for tgt, write_list in sorted(target_write_chunks.items()):
        if len(write_list) >= 3:
            first_c = write_list[0]
            lines_desc = ", ".join(f"L{c['start_line']}" for c in write_list[:6])
            findings.append(
                _make_finding(
                    script_id=script_id,
                    chunk=first_c,
                    rule_code="S013",
                    description=(
                        f"脚本内共有 {len(write_list)} 段独立逻辑反复写入目标表 `{tgt}`（位置：{lines_desc}），"
                        "极易多次重复扫描相同底层大表并造成逻辑分散。"
                    ),
                    evidence_snippet=f"Target {tgt} written at chunks {[c['start_line'] for c in write_list]}",
                    suggestion="使用 `WITH CTE` 共享基础数据集，或利用 `CASE WHEN` / `UNION ALL` 将多分支合并为单次全表扫描批量插入。",
                    expected_benefit=f"减少对基础源表 {len(write_list) - 1} 倍重复 IO 扫描与上下文切换开销。",
                    related_tables=[tgt],
                )
            )

    return findings
