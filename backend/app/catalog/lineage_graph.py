"""In-memory and persisted cross-script lineage DAG builder, cycle/conflict detector, and blast-radius tracer."""

from __future__ import annotations

from collections import defaultdict, deque
import uuid
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.infra.db_tables import (
    LineageEdgeModel,
    ScriptChunkModel,
    ScriptFileModel,
    ScriptProjectModel,
    TableSchemaModel,
)


def _norm(name: str) -> str:
    return (name or "").strip().strip('"').strip("`").split(".")[-1].upper()


def _is_temp_table_name(table_name: str) -> bool:
    u = table_name.upper()
    return u.endswith(("_TMP", "_TEMP")) or u.startswith(("TMP_", "TEMP_"))


def build_lineage_topology(
    scripts: Iterable[Dict[str, Any]],
    chunks: Iterable[Dict[str, Any]],
    schemas: Iterable[Dict[str, Any]] = (),
) -> Dict[str, Any]:
    """Compute cross-script DAG edges, conflicts, cycles, hubs, long chains, orphans, and duplicate clusters."""
    script_map: Dict[str, Dict[str, Any]] = {}
    for s in scripts:
        sid = str(s["id"])
        script_map[sid] = {
            "id": sid,
            "filename": s["filename"],
            "relative_path": s.get("relative_path", s["filename"]),
            "file_kind": s.get("file_kind", "SCRIPT"),
            "target_tables": [_norm(t) for t in (s.get("target_tables") or []) if _norm(t)],
            "source_tables": [_norm(t) for t in (s.get("source_tables") or []) if _norm(t)],
        }

    # Index table -> writer scripts, reader scripts, truncator scripts
    table_writers: Dict[str, Set[str]] = defaultdict(set)
    table_readers: Dict[str, Set[str]] = defaultdict(set)
    table_truncators: Dict[str, Set[str]] = defaultdict(set)
    write_kinds = {"INSERT", "MERGE", "UPDATE", "CTAS", "CREATE_TABLE"}

    chunks_by_script: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for ch in chunks:
        sid = str(ch["script_id"])
        if sid not in script_map:
            continue
        chunks_by_script[sid].append(ch)
        if script_map[sid]["file_kind"] == "DDL":
            # DDL files define schemas rather than runtime data writers/readers
            continue
        kind = str(ch.get("statement_kind", "OTHER")).upper()
        feats = ch.get("ast_features") or {}
        targets = [_norm(t) for t in (ch.get("target_tables") or []) if _norm(t)]
        sources = [_norm(t) for t in (ch.get("source_tables") or []) if _norm(t)]

        if kind == "TRUNCATE" or feats.get("has_truncate"):
            for t in targets:
                table_truncators[t].add(sid)
        elif kind in write_kinds:
            for t in targets:
                table_writers[t].add(sid)

        for s_tbl in sources:
            table_readers[s_tbl].add(sid)

    # Also honor script-level table lists in case chunks had aggregate info
    for sid, sdata in script_map.items():
        if sdata["file_kind"] == "DDL":
            # DDL defines tables rather than ETL populating data flows
            continue
        for s_tbl in sdata["source_tables"]:
            table_readers[s_tbl].add(sid)
        if not chunks_by_script.get(sid):
            for t_tbl in sdata["target_tables"]:
                table_writers[t_tbl].add(sid)

    edges: List[Dict[str, str]] = []
    seen_edges: Set[Tuple[str, str, str, str]] = set()

    def _add_edge(up: str, down: str, table: str, kind: str) -> None:
        if up == down:
            return
        key = (up, down, table, kind)
        if key not in seen_edges:
            seen_edges.add(key)
            edges.append(
                {
                    "upstream_script_id": up,
                    "downstream_script_id": down,
                    "via_table": table,
                    "edge_kind": kind,
                }
            )

    # 1. DATA_FLOW edges: producer writes T, consumer reads T
    for tbl, writers in sorted(table_writers.items()):
        readers = table_readers.get(tbl, set())
        for up in sorted(writers):
            for down in sorted(readers):
                if up != down:
                    _add_edge(up, down, tbl, "DATA_FLOW")

    # 2. WRITE_CONFLICT: >=2 scripts write the same persistent table
    write_conflict_tables: List[Dict[str, Any]] = []
    for tbl, writers in sorted(table_writers.items()):
        if len(writers) >= 2 and not _is_temp_table_name(tbl):
            ordered = sorted(writers)
            write_conflict_tables.append(
                {
                    "table": tbl,
                    "script_ids": ordered,
                    "filenames": [script_map[sid]["filename"] for sid in ordered],
                }
            )
            for i in range(len(ordered) - 1):
                _add_edge(ordered[i], ordered[i + 1], tbl, "WRITE_CONFLICT")

    # 3. TRUNCATE_CONFLICT: script A truncates T, and another script B reads or writes T
    truncate_conflicts: List[Dict[str, Any]] = []
    for tbl, truncators in sorted(table_truncators.items()):
        others = (table_readers.get(tbl, set()) | table_writers.get(tbl, set())) - truncators
        if others:
            for tr in sorted(truncators):
                for other in sorted(others):
                    _add_edge(tr, other, tbl, "TRUNCATE_CONFLICT")
            truncate_conflicts.append(
                {
                    "table": tbl,
                    "truncators": [script_map[sid]["filename"] for sid in sorted(truncators)],
                    "affected": [script_map[sid]["filename"] for sid in sorted(others)],
                }
            )

    # 4. Cycle detection & longest chain on DATA_FLOW graph
    adj: Dict[str, Set[str]] = defaultdict(set)
    indegree: Dict[str, int] = {sid: 0 for sid in script_map}
    for e in edges:
        if e["edge_kind"] != "DATA_FLOW":
            continue
        u, v = e["upstream_script_id"], e["downstream_script_id"]
        if v not in adj[u]:
            adj[u].add(v)
            indegree[v] += 1

    # Kahn topological sort
    q = deque([sid for sid, deg in indegree.items() if deg == 0])
    topo_order: List[str] = []
    dist: Dict[str, int] = {sid: 0 for sid in script_map}
    parent_chain: Dict[str, Optional[str]] = {sid: None for sid in script_map}

    while q:
        u = q.popleft()
        topo_order.append(u)
        for v in sorted(adj.get(u, ())):
            if dist[u] + 1 > dist[v]:
                dist[v] = dist[u] + 1
                parent_chain[v] = u
            indegree[v] -= 1
            if indegree[v] == 0:
                q.append(v)

    cyclic_nodes = sorted(set(script_map.keys()) - set(topo_order))
    cycles: List[List[str]] = []
    if cyclic_nodes:
        # Extract explicit directed cycles among cyclic_nodes using bounded DFS
        cyclic_set = set(cyclic_nodes)
        visited_cycles: Set[Tuple[str, ...]] = set()
        for start in cyclic_nodes:
            stack: List[Tuple[str, List[str]]] = [(start, [start])]
            seen_in_dfs: Set[str] = set()
            while stack:
                curr, path = stack.pop()
                for nxt in sorted(adj.get(curr, ())):
                    if nxt == start and len(path) >= 2:
                        # Canonical cycle rotation
                        min_idx = min(range(len(path)), key=lambda idx: path[idx])
                        rot = tuple(path[min_idx:] + path[:min_idx])
                        if rot not in visited_cycles:
                            visited_cycles.add(rot)
                            cycles.append([script_map[sid]["filename"] for sid in rot])
                    elif nxt in cyclic_set and nxt not in seen_in_dfs and len(path) < 12:
                        seen_in_dfs.add(nxt)
                        stack.append((nxt, path + [nxt]))

    # Longest chain in acyclic component
    longest_chain_names: List[str] = []
    longest_len = 0
    if topo_order:
        best_end = max(topo_order, key=lambda sid: dist[sid])
        longest_len = dist[best_end]
        if longest_len >= 1:
            rev_path: List[str] = []
            cur: Optional[str] = best_end
            while cur is not None:
                rev_path.append(script_map[cur]["filename"])
                cur = parent_chain[cur]
            longest_chain_names = list(reversed(rev_path))

    # 5. Hub tables & Orphan / Dead / Missing DDL tables
    defined_tables = {_norm(str(sc.get("table_name", ""))) for sc in schemas if _norm(str(sc.get("table_name", "")))}
    all_tables = sorted(set(table_writers.keys()) | set(table_readers.keys()) | set(table_truncators.keys()) | defined_tables)

    hub_tables: List[Dict[str, Any]] = []
    dead_tables: List[str] = []
    missing_ddl_tables: List[str] = []

    for tbl in all_tables:
        w = len(table_writers.get(tbl, set()))
        r = len(table_readers.get(tbl, set()))
        if (w >= 2 and r >= 2) or (w + r >= 4) or r >= 4:
            hub_tables.append(
                {
                    "table": tbl,
                    "writer_count": w,
                    "reader_count": r,
                    "writers": [script_map[sid]["filename"] for sid in sorted(table_writers.get(tbl, set()))],
                    "readers": [script_map[sid]["filename"] for sid in sorted(table_readers.get(tbl, set()))],
                }
            )
        if tbl in table_writers and tbl not in table_readers and not _is_temp_table_name(tbl):
            # Written by ETL but never read downstream (candidate sink/orphan check — informational unless staging/temp unused)
            if tbl.endswith(("_STG", "_STAGE", "_MID", "_LOG")):
                dead_tables.append(tbl)
        elif tbl in defined_tables and tbl not in table_writers and tbl not in table_readers:
            dead_tables.append(tbl)

        if defined_tables and tbl in (set(table_writers.keys()) | set(table_readers.keys())) and tbl not in defined_tables:
            missing_ddl_tables.append(tbl)

    # 6. Duplicate SQL logic clusters across distinct scripts
    fp_groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for sid, clist in chunks_by_script.items():
        for ch in clist:
            kind = str(ch.get("statement_kind", "OTHER")).upper()
            raw = str(ch.get("raw_sql", ""))
            fp = str(ch.get("ast_fingerprint", ""))
            if not fp or kind not in ("INSERT", "MERGE", "UPDATE", "DELETE", "CTAS", "SELECT"):
                continue
            if len(raw.strip()) < 80:
                continue
            fp_groups[fp].append(
                {
                    "script_id": sid,
                    "filename": script_map[sid]["filename"],
                    "chunk_id": ch.get("id"),
                    "start_line": ch.get("start_line"),
                    "end_line": ch.get("end_line"),
                    "statement_kind": kind,
                    "target_tables": ch.get("target_tables") or [],
                }
            )

    duplicate_clusters: List[Dict[str, Any]] = []
    for fp, occs in fp_groups.items():
        distinct_scripts = {o["script_id"] for o in occs}
        if len(distinct_scripts) >= 2:
            duplicate_clusters.append(
                {
                    "ast_fingerprint": fp,
                    "occurrence_count": len(occs),
                    "script_count": len(distinct_scripts),
                    "occurrences": occs,
                }
            )

    return {
        "nodes": [
            {
                "id": sid,
                "filename": sdata["filename"],
                "file_kind": sdata["file_kind"],
                "target_tables": sdata["target_tables"],
                "source_tables": sdata["source_tables"],
            }
            for sid, sdata in sorted(script_map.items(), key=lambda kv: kv[1]["filename"])
        ],
        "edges": edges,
        "cycles": cycles,
        "longest_chain": longest_chain_names,
        "longest_chain_hops": longest_len,
        "write_conflict_tables": write_conflict_tables,
        "truncate_conflicts": truncate_conflicts,
        "hub_tables": sorted(hub_tables, key=lambda h: -(h["writer_count"] + h["reader_count"])),
        "dead_or_orphan_tables": sorted(dead_tables),
        "missing_ddl_tables": sorted(missing_ddl_tables),
        "duplicate_clusters": duplicate_clusters,
        "node_count": len(script_map),
        "edge_count": len(edges),
    }


def compute_blast_radius(
    target_script_id: str,
    topology: Dict[str, Any],
) -> Dict[str, Any]:
    """Trace multi-hop downstream blast radius starting at `target_script_id`."""
    nodes = {n["id"]: n for n in topology.get("nodes", [])}
    adj: Dict[str, List[Tuple[str, str, str]]] = defaultdict(list)
    for e in topology.get("edges", []):
        if e["edge_kind"] in ("DATA_FLOW", "TRUNCATE_CONFLICT", "WRITE_CONFLICT"):
            adj[e["upstream_script_id"]].append((e["downstream_script_id"], e["via_table"], e["edge_kind"]))

    visited_hop: Dict[str, int] = {target_script_id: 0}
    queue = deque([target_script_id])
    impacted_scripts: List[Dict[str, Any]] = []
    impacted_tables: Set[str] = set()

    # Include direct tables written by target script
    if target_script_id in nodes:
        for t in nodes[target_script_id].get("target_tables", []):
            impacted_tables.add(t)

    while queue:
        curr = queue.popleft()
        hop = visited_hop[curr]
        for down_id, via_tbl, kind in adj.get(curr, []):
            impacted_tables.add(via_tbl)
            if down_id not in visited_hop:
                visited_hop[down_id] = hop + 1
                queue.append(down_id)
                down_node = nodes.get(down_id, {})
                impacted_scripts.append(
                    {
                        "script_id": down_id,
                        "filename": down_node.get("filename", down_id),
                        "hop": hop + 1,
                        "via_table": via_tbl,
                        "edge_kind": kind,
                    }
                )

    direct_scripts = [s for s in impacted_scripts if s["hop"] == 1]
    transitive_scripts = [s for s in impacted_scripts if s["hop"] >= 2]
    max_hop = max((s["hop"] for s in impacted_scripts), default=0)

    return {
        "target_script_id": target_script_id,
        "direct_script_count": len(direct_scripts),
        "transitive_script_count": len(transitive_scripts),
        "total_impacted_scripts": len(impacted_scripts),
        "impacted_tables": sorted(impacted_tables),
        "max_hop": max_hop,
        "impacted_scripts": sorted(impacted_scripts, key=lambda s: (s["hop"], s["filename"])),
    }


def rebuild_project_lineage(session: Session, project_id: str) -> Dict[str, Any]:
    """Recompute and persist `LineageEdgeModel` rows and `ScriptProjectModel.summary_json`."""
    scripts = session.query(ScriptFileModel).filter(ScriptFileModel.project_id == project_id).all()
    chunks = session.query(ScriptChunkModel).filter(ScriptChunkModel.project_id == project_id).all()
    schemas = session.query(TableSchemaModel).filter(TableSchemaModel.project_id == project_id).all()

    topology = build_lineage_topology(
        scripts=[
            {
                "id": s.id,
                "filename": s.filename,
                "relative_path": s.relative_path,
                "file_kind": s.file_kind,
                "target_tables": s.target_tables_json or [],
                "source_tables": s.source_tables_json or [],
            }
            for s in scripts
        ],
        chunks=[
            {
                "id": c.id,
                "script_id": c.script_id,
                "start_line": c.start_line,
                "end_line": c.end_line,
                "statement_kind": c.statement_kind,
                "target_tables": c.target_tables_json or [],
                "source_tables": c.source_tables_json or [],
                "ast_features": c.ast_features_json or {},
                "ast_fingerprint": c.ast_fingerprint,
                "raw_sql": c.raw_sql,
            }
            for c in chunks
        ],
        schemas=[{"table_name": sc.table_name} for sc in schemas],
    )

    session.execute(delete(LineageEdgeModel).where(LineageEdgeModel.project_id == project_id))
    for edge in topology["edges"]:
        session.add(
            LineageEdgeModel(
                id=str(uuid.uuid4()),
                project_id=project_id,
                upstream_script_id=edge["upstream_script_id"],
                downstream_script_id=edge["downstream_script_id"],
                via_table=edge["via_table"],
                edge_kind=edge["edge_kind"],
            )
        )

    project = session.get(ScriptProjectModel, project_id)
    if project is not None:
        project.summary_json = {
            "cycles": topology["cycles"],
            "longest_chain": topology["longest_chain"],
            "longest_chain_hops": topology["longest_chain_hops"],
            "write_conflict_tables": topology["write_conflict_tables"],
            "truncate_conflicts": topology["truncate_conflicts"],
            "hub_tables": topology["hub_tables"],
            "dead_or_orphan_tables": topology["dead_or_orphan_tables"],
            "missing_ddl_tables": topology["missing_ddl_tables"],
            "duplicate_clusters": topology["duplicate_clusters"],
            "edge_count": topology["edge_count"],
        }
        project.table_count = len(schemas)

    session.commit()
    return topology
