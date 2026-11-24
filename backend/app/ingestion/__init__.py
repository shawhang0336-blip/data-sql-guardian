"""Layer 3 ingestion public surface delegating to pure-offline parsers."""

from .parsers import (
    AstFeatures,
    ChunkRecord,
    CollectedScript,
    JoinCondition,
    ParsedScript,
    TableSchemaRecord,
    aggregate_script_features,
    analyze_chunk_ast,
    chunk_sql_script,
    collect_directory_scripts,
    collect_real_sac_scripts,
    compute_normalized_fingerprint,
    detect_sql_dialect,
    extract_zip_safely,
    normalize_table_identifier,
    parse_ddl_statements,
)

__all__ = [
    "AstFeatures",
    "ChunkRecord",
    "CollectedScript",
    "JoinCondition",
    "ParsedScript",
    "TableSchemaRecord",
    "aggregate_script_features",
    "analyze_chunk_ast",
    "chunk_sql_script",
    "collect_directory_scripts",
    "collect_real_sac_scripts",
    "compute_normalized_fingerprint",
    "detect_sql_dialect",
    "extract_zip_safely",
    "normalize_table_identifier",
    "parse_ddl_statements",
]
