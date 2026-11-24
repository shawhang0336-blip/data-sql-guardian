"""6 deterministic project-wide optimization rules (pure offline, consuming lineage topology)."""

from __future__ import annotations

from typing import Any, Dict, List


PROJECT_RULE_CATALOG: Dict[str, Dict[str, str]] = {
    "P001": {"severity": "P0", "category": "RELIABILITY", "title": "跨脚本并发写同一主表或 TRUNCATE 读/写冲突"},
    "P002": {"severity": "P0", "category": "RELIABILITY", "title": "项目血缘有向图存在循环依赖（拓扑死锁风险）"},
    "P003": {"severity": "P1", "category": "MAINTAINABILITY", "title": "跨脚本存在高度重复的 SQL 处理逻辑簇"},
    "P004": {"severity": "P1", "category": "PERFORMANCE", "title": "串行作业依赖链路过长（长尾关键路径延迟风险）"},
    "P005": {"severity": "P2", "category": "ARCHITECTURE", "title": "高扇入/高扇出枢纽表瓶颈（中心表耦合过重）"},
    "P006": {"severity": "P2", "category": "GOVERNANCE", "title": "存在潜在冗余/未消费临时表或缺失 DDL 定义的数据表"},
}


def _make_project_finding(
    *,
    rule_code: str,
    description: str,
    evidence_snippet: str,
    suggestion: str,
    expected_benefit: str,
    related_tables: List[str],
    related_scripts: List[str],
) -> Dict[str, Any]:
    meta = PROJECT_RULE_CATALOG[rule_code]
    return {
        "scope": "PROJECT",
        "script_id": None,
        "chunk_id": None,
        "rule_code": rule_code,
        "category": meta["category"],
        "severity": meta["severity"],
        "title": meta["title"],
        "description": description,
        "evidence_snippet": evidence_snippet[:500],
        "start_line": None,
        "end_line": None,
        "related_tables": related_tables,
        "related_scripts": related_scripts,
        "suggestion": suggestion,
        "rewritten_sql": None,
        "expected_benefit": expected_benefit,
    }


def evaluate_project_rules(
    topology: Dict[str, Any],
    *,
    max_chain_hops: int = 3,
) -> List[Dict[str, Any]]:
    """Run P001–P006 project-wide structural rules against computed `build_lineage_topology` output."""
    findings: List[Dict[str, Any]] = []
    name_to_id = {n["filename"]: n["id"] for n in topology.get("nodes", [])}

    # P001: Shared-table write conflicts & TRUNCATE conflicts
    for wc in topology.get("write_conflict_tables", []):
        tbl = wc["table"]
        files = wc["filenames"]
        sids = wc.get("script_ids") or [name_to_id[f] for f in files if f in name_to_id]
        findings.append(
            _make_project_finding(
                rule_code="P001",
                description=(
                    f"主表 `{tbl}` 同时被 {len(files)} 个脚本写入（{', '.join(files)}），"
                    "若调度并发执行可能引发表锁等待、数据互相覆盖或统计口径不一致。"
                ),
                evidence_snippet=f"Table {tbl} <= writers: {', '.join(files)}",
                suggestion=f"梳理 `{tbl}` 的唯一主责加工脚本（Single Writer），将其余写入拆分为独立中间表或明确调度锁序依赖。",
                expected_benefit="彻底消除并发写锁冲突与口径覆盖风险。",
                related_tables=[tbl],
                related_scripts=sids,
            )
        )

    for tc in topology.get("truncate_conflicts", []):
        tbl = tc["table"]
        truncs = tc["truncators"]
        affected = tc["affected"]
        sids = [name_to_id[f] for f in (truncs + affected) if f in name_to_id]
        findings.append(
            _make_project_finding(
                rule_code="P001",
                description=(
                    f"表 `{tbl}` 在 {', '.join(truncs)} 中被执行 `TRUNCATE TABLE`，"
                    f"同时被下游/并发脚本 {', '.join(affected)} 读取或写入，存在严重清表并发冲突与空数风险。"
                ),
                evidence_snippet=f"TRUNCATE {tbl} by {', '.join(truncs)} impacts {', '.join(affected)}",
                suggestion=f"在调度平台固化 `{', '.join(truncs)} -> {', '.join(affected)}` 强依赖，或改用按批号/日期分区增量写入隔离。",
                expected_benefit="防止下游在清理窗口读到空表或部分提交状态。",
                related_tables=[tbl],
                related_scripts=sids,
            )
        )

    # P002: Lineage DAG Cycles
    for cyc in topology.get("cycles", []):
        sids = [name_to_id[f] for f in cyc if f in name_to_id]
        findings.append(
            _make_project_finding(
                rule_code="P002",
                description=f"表级血缘图检测到脚本级环形依赖：{' -> '.join(cyc + [cyc[0]])}，调度系统无法按稳定拓扑序回溯重跑。",
                evidence_snippet="Cycle: " + " -> ".join(cyc),
                suggestion="将双向读写表按快照日期或分层（ODS/DWD/DWS）解耦，打破环路，形成严格的 DAG 有向无环图。",
                expected_benefit="恢复全链路自动化拓扑调度与一键历史重跑能力。",
                related_tables=[],
                related_scripts=sids,
            )
        )

    # P003: Cross-script duplicate logic clusters
    for cl in topology.get("duplicate_clusters", []):
        occs = cl.get("occurrences", [])
        files = sorted({o["filename"] for o in occs})
        sids = sorted({o["script_id"] for o in occs})
        tables = sorted({t for o in occs for t in (o.get("target_tables") or [])})
        findings.append(
            _make_project_finding(
                rule_code="P003",
                description=(
                    f"指纹 `{cl['ast_fingerprint']}` 对应的 SQL 块在 {len(files)} 个脚本中重复出现 "
                    f"({', '.join(files)})，共 {cl['occurrence_count']} 处结构高度一致代码段。"
                ),
                evidence_snippet=", ".join(f"{o['filename']}:L{o['start_line']}" for o in occs[:6]),
                suggestion="抽取为公共存储过程、通用视图或标准中间层宽表，避免多处复制粘贴导致的口径漂移。",
                expected_benefit="降低多脚本维护成本与重复计算资源消耗。",
                related_tables=tables,
                related_scripts=sids,
            )
        )

    # P004: Long serial dependency chain
    chain = topology.get("longest_chain", [])
    hops = int(topology.get("longest_chain_hops", 0))
    if hops >= max_chain_hops and len(chain) >= 2:
        sids = [name_to_id[f] for f in chain if f in name_to_id]
        findings.append(
            _make_project_finding(
                rule_code="P004",
                description=(
                    f"检测到长达 {hops} 跳（{len(chain)} 个脚本）的串行依赖链："
                    f"{' -> '.join(chain)}，关键路径 SLA 易受单点波动影响。"
                ),
                evidence_snippet="Longest chain: " + " -> ".join(chain),
                suggestion="识别链路上仅做简单透传/零星字段补齐的中间脚本进行垂直合并，或将可独立分支下游并行化。",
                expected_benefit="压缩端到端批处理关键路径时长，提升 T+1 准时交付率。",
                related_tables=[],
                related_scripts=sids,
            )
        )

    # P005: Hub bottleneck tables
    for hub in topology.get("hub_tables", []):
        tbl = hub["table"]
        files = sorted(set(hub.get("writers", []) + hub.get("readers", [])))
        sids = [name_to_id[f] for f in files if f in name_to_id]
        findings.append(
            _make_project_finding(
                rule_code="P005",
                description=(
                    f"表 `{tbl}` 为核心枢纽表（被 {hub['writer_count']} 个脚本写入、{hub['reader_count']} 个脚本读取），"
                    "一旦该表发生结构变更或数据延迟，将产生大范围级联影响。"
                ),
                evidence_snippet=f"Hub {tbl}: writers={hub['writers']}, readers={hub['readers']}",
                suggestion=f"为 `{tbl}` 制定独立契约视图（Stable View）及严格的上线变更门禁，并评估热点分区索引。",
                expected_benefit="降低中心表耦合度与高频查询锁竞争。",
                related_tables=[tbl],
                related_scripts=sids,
            )
        )

    # P006: Dead/orphan staging tables or missing DDL definitions
    dead = topology.get("dead_or_orphan_tables", [])
    missing = topology.get("missing_ddl_tables", [])
    if dead or missing:
        parts = []
        if dead:
            parts.append(f"潜在孤儿/僵尸中间表 {len(dead)} 张（如 {', '.join(dead[:6])}）")
        if missing:
            parts.append(f"引用但缺失 DDL 定义表 {len(missing)} 张（如 {', '.join(missing[:6])}）")
        findings.append(
            _make_project_finding(
                rule_code="P006",
                description="表资产治理扫描发现：" + "；".join(parts) + "。",
                evidence_snippet=f"orphan={dead[:5]}, missing_ddl={missing[:5]}",
                suggestion="确认僵尸中间表是否可下线清理，并补全缺失源表/目标表的标准 DDL 脚本纳管以实现完整字段级影响评估。",
                expected_benefit="清理无效存储占用并完善全库资产元数据闭环。",
                related_tables=(dead[:10] + missing[:10]),
                related_scripts=[],
            )
        )

    return findings
