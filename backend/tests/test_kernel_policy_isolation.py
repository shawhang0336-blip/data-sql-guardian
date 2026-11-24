"""AST architecture guardrails enforcing ds_agent's 8-layer DAG, kernel/policy boundaries, and pure-offline subpackages."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, List, Set, Tuple

APP_ROOT = Path(__file__).resolve().parents[1] / "app"

LAYER_RANK: Dict[str, int] = {
    "infra": 0,
    "kernel": 1,
    "policy": 2,
    "ingestion": 3,
    "catalog": 4,
    "analysis": 5,
    "review": 6,
    "api": 7,
    "main": 8,
}

ALLOWED_ROOT_ENTRIES = {
    "__init__.py",
    "__pycache__",
    "main.py",
    "infra",
    "kernel",
    "policy",
    "ingestion",
    "catalog",
    "analysis",
    "review",
    "api",
}

ALLOWED_KERNEL_FILES = {
    "__init__.py",
    "__pycache__",
    "plan.py",
    "bus.py",
    "event_log.py",
    "interrupt.py",
    "run.py",
    "scheduler.py",
}

ALLOWED_POLICY_FILES = {
    "__init__.py",
    "__pycache__",
    "ds_plan.py",
    "ds_bus.py",
    "ds_channels.py",
}

PURE_OFFLINE_FORBIDDEN_PREFIXES = (
    "sqlalchemy",
    "app.infra.db",
    "app.infra.llm",
    "app.kernel",
    "app.policy",
    "app.catalog",
    "app.review",
    "app.api",
)


def _layer_of_file(path: Path) -> str:
    rel = path.relative_to(APP_ROOT)
    if rel.name == "__init__.py" and len(rel.parts) == 1:
        return "infra"
    if rel.name == "main.py" and len(rel.parts) == 1:
        return "main"
    return rel.parts[0]


def _extract_imports(py_file: Path) -> List[str]:
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    imports: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                pkg_parts = list(py_file.relative_to(APP_ROOT).parts[:-1])
                up = node.level - 1
                base_parts = pkg_parts[: len(pkg_parts) - up] if up <= len(pkg_parts) else []
                prefix = ".".join(["app", *base_parts])
                mod = f"{prefix}.{node.module}" if node.module else prefix
                imports.append(mod)
            elif node.module:
                imports.append(node.module)
    return imports


def test_app_root_layout_and_kernel_policy_membership():
    root_entries = {p.name for p in APP_ROOT.iterdir()}
    assert root_entries.issubset(ALLOWED_ROOT_ENTRIES), f"Unexpected entries in app/: {root_entries - ALLOWED_ROOT_ENTRIES}"

    kernel_entries = {p.name for p in (APP_ROOT / "kernel").iterdir()}
    assert kernel_entries.issubset(ALLOWED_KERNEL_FILES), f"Unexpected files in kernel/: {kernel_entries - ALLOWED_KERNEL_FILES}"

    policy_entries = {p.name for p in (APP_ROOT / "policy").iterdir()}
    assert policy_entries.issubset(ALLOWED_POLICY_FILES), f"Unexpected files in policy/: {policy_entries - ALLOWED_POLICY_FILES}"


def test_strict_8_layer_one_way_dag():
    violations: List[Tuple[str, str, str]] = []
    for py_file in sorted(APP_ROOT.rglob("*.py")):
        src_layer = _layer_of_file(py_file)
        if src_layer not in LAYER_RANK:
            continue
        src_rank = LAYER_RANK[src_layer]
        for mod in _extract_imports(py_file):
            if not mod.startswith("app."):
                continue
            parts = mod.split(".")
            if len(parts) < 2:
                continue
            tgt_layer = parts[1]
            if tgt_layer not in LAYER_RANK:
                continue
            tgt_rank = LAYER_RANK[tgt_layer]
            if tgt_rank > src_rank:
                violations.append((str(py_file.relative_to(APP_ROOT)), mod, f"{src_layer}({src_rank}) -> {tgt_layer}({tgt_rank})"))
    assert not violations, f"Layer DAG violations detected: {violations}"


def test_pure_offline_subpackages_zero_db_or_llm():
    pure_dirs = [APP_ROOT / "ingestion" / "parsers", APP_ROOT / "analysis" / "core"]
    violations: List[Tuple[str, str]] = []
    for pdir in pure_dirs:
        assert pdir.exists(), f"Missing pure dir: {pdir}"
        for py_file in sorted(pdir.rglob("*.py")):
            for mod in _extract_imports(py_file):
                if any(mod == pref or mod.startswith(pref + ".") for pref in PURE_OFFLINE_FORBIDDEN_PREFIXES):
                    violations.append((str(py_file.relative_to(APP_ROOT)), mod))
    assert not violations, f"Pure offline purity violations: {violations}"


def test_api_routers_zero_cross_domain_imports():
    domains = {"platform", "workspace", "analysis", "review"}
    violations: List[Tuple[str, str]] = []
    for dom in domains:
        dom_dir = APP_ROOT / "api" / dom
        for py_file in sorted(dom_dir.rglob("*.py")):
            for mod in _extract_imports(py_file):
                if mod.startswith("app.api."):
                    parts = mod.split(".")
                    if len(parts) >= 3 and parts[2] in domains and parts[2] != dom:
                        violations.append((str(py_file.relative_to(APP_ROOT)), mod))
    assert not violations, f"Cross-domain API router imports: {violations}"


def test_policy_import_has_zero_global_bus_or_reducer_side_effects():
    from app.kernel.bus import global_bus
    from app.kernel.event_log import _DEFAULT_REDUCER

    orig_adj_count = len(global_bus()._adjudicators)  # noqa: SLF001
    orig_reducer = _DEFAULT_REDUCER

    import app.policy  # noqa: F401
    from app.policy import default_bus

    assert len(global_bus()._adjudicators) == orig_adj_count  # noqa: SLF001
    from app.kernel.event_log import _DEFAULT_REDUCER as after_reducer

    assert after_reducer is orig_reducer
    local_bus = default_bus()
    assert len(local_bus._adjudicators) == 5  # noqa: SLF001
