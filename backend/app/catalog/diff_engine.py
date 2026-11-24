"""Unified line diff + SQL chunk/AST structural delta calculator for pre-release review."""

from __future__ import annotations

import difflib
from typing import Any, Dict, List

from app.ingestion import (
    aggregate_script_features,
    analyze_chunk_ast,
    chunk_sql_script,
)


def compute_script_ast_diff(
    old_sql: str,
    new_sql: str,
    *,
    filename: str = "script.sql",
    dialect: str = "oracle",
) -> Dict[str, Any]:
    """Compare old vs new script texts at line-diff, chunk, table, and AST-feature granularity."""
    old_lines = old_sql.splitlines() if old_sql else []
    new_lines = new_sql.splitlines() if new_sql else []
    diff_lines = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
            lineterm="",
        )
    )
    added_lines = sum(1 for ln in diff_lines if ln.startswith("+") and not ln.startswith("+++"))
    removed_lines = sum(1 for ln in diff_lines if ln.startswith("-") and not ln.startswith("---"))

    old_parsed = chunk_sql_script(old_sql, filename=filename, dialect=dialect) if old_sql.strip() else None
    new_parsed = chunk_sql_script(new_sql, filename=filename, dialect=dialect)

    old_feats = (
        [analyze_chunk_ast(ch.effective_sql, statement_kind=ch.statement_kind, dialect=old_parsed.dialect) for ch in old_parsed.chunks]
        if old_parsed
        else []
    )
    new_feats = [
        analyze_chunk_ast(ch.effective_sql, statement_kind=ch.statement_kind, dialect=new_parsed.dialect)
        for ch in new_parsed.chunks
    ]

    old_agg = aggregate_script_features(old_feats) if old_feats else {"target_tables": [], "source_tables": []}
    new_agg = aggregate_script_features(new_feats)

    old_targets = set(old_agg["target_tables"])
    new_targets = set(new_agg["target_tables"])
    old_sources = set(old_agg["source_tables"])
    new_sources = set(new_agg["source_tables"])

    old_fps = {f.ast_fingerprint: idx for idx, f in enumerate(old_feats)}
    new_fps = {f.ast_fingerprint: idx for idx, f in enumerate(new_feats)}

    added_chunks: List[Dict[str, Any]] = []
    removed_chunks: List[Dict[str, Any]] = []
    for idx, ch in enumerate(new_parsed.chunks):
        fp = new_feats[idx].ast_fingerprint
        if fp not in old_fps:
            added_chunks.append(
                {
                    "chunk_index": ch.chunk_index,
                    "start_line": ch.start_line,
                    "end_line": ch.end_line,
                    "statement_kind": ch.statement_kind,
                    "target_tables": new_feats[idx].target_tables,
                    "ast_fingerprint": fp,
                }
            )
    if old_parsed:
        for idx, ch in enumerate(old_parsed.chunks):
            fp = old_feats[idx].ast_fingerprint
            if fp not in new_fps:
                removed_chunks.append(
                    {
                        "chunk_index": ch.chunk_index,
                        "start_line": ch.start_line,
                        "end_line": ch.end_line,
                        "statement_kind": ch.statement_kind,
                        "target_tables": old_feats[idx].target_tables,
                        "ast_fingerprint": fp,
                    }
                )

    exception_handler_removed = bool(
        old_parsed and old_parsed.has_exception_handler and not new_parsed.has_exception_handler
    )
    new_truncates = [
        t
        for f in new_feats
        if f.has_truncate
        for t in f.target_tables
    ]
    new_dynamic_sql_count = sum(1 for ch in new_parsed.chunks if ch.is_dynamic_sql)

    return {
        "unified_diff": "\n".join(diff_lines),
        "added_lines": added_lines,
        "removed_lines": removed_lines,
        "old_line_count": len(old_lines),
        "new_line_count": len(new_lines),
        "old_chunk_count": len(old_parsed.chunks) if old_parsed else 0,
        "new_chunk_count": len(new_parsed.chunks),
        "old_target_tables": sorted(old_targets),
        "new_target_tables": sorted(new_targets),
        "added_target_tables": sorted(new_targets - old_targets),
        "removed_target_tables": sorted(old_targets - new_targets),
        "added_source_tables": sorted(new_sources - old_sources),
        "removed_source_tables": sorted(old_sources - new_sources),
        "added_chunks": added_chunks,
        "removed_chunks": removed_chunks,
        "old_commit_count": old_parsed.commit_count if old_parsed else 0,
        "new_commit_count": new_parsed.commit_count,
        "exception_handler_removed": exception_handler_removed,
        "new_truncates": sorted(set(new_truncates)),
        "new_dynamic_sql_count": new_dynamic_sql_count,
        "new_parsed_script": new_parsed,
        "new_chunk_features": new_feats,
    }
