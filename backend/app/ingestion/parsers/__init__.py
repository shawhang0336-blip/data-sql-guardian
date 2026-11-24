"""Pure offline ingestion parsers and loaders package (zero DB/LLM/upper imports)."""

from .archive_loader import (
    CollectedScript,
    collect_directory_scripts,
    collect_real_sac_scripts,
    extract_zip_safely,
    normalize_table_identifier,
)
from .ast_analyzer import (
    AstFeatures,
    JoinCondition,
    aggregate_script_features,
    analyze_chunk_ast,
    compute_normalized_fingerprint,
)
from .ddl_parser import TableSchemaRecord, parse_ddl_statements
from .dialect_chunker import (
    ChunkRecord,
    ParsedScript,
    chunk_sql_script,
    detect_sql_dialect,
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
