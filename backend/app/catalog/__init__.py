"""Layer 4 catalog public interface: project import, schema registry, lineage DAG, AST diff."""

from .diff_engine import compute_script_ast_diff
from .lineage_graph import (
    build_lineage_topology,
    compute_blast_radius,
    rebuild_project_lineage,
)
from .project_service import (
    delete_project,
    get_project_detail,
    get_script_detail,
    import_project_from_directory,
    import_project_from_zip,
    import_sac_sample_project,
    ingest_collected_scripts,
    list_project_schemas,
    list_project_scripts,
    list_projects,
    load_script_raw_text,
    serialize_project,
    serialize_script,
)

__all__ = [
    "build_lineage_topology",
    "compute_blast_radius",
    "compute_script_ast_diff",
    "delete_project",
    "get_project_detail",
    "get_script_detail",
    "import_project_from_directory",
    "import_project_from_zip",
    "import_sac_sample_project",
    "ingest_collected_scripts",
    "list_project_schemas",
    "list_project_scripts",
    "list_projects",
    "load_script_raw_text",
    "rebuild_project_lineage",
    "serialize_project",
    "serialize_script",
]
