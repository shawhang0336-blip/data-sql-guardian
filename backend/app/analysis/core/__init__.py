"""Pure offline deterministic static optimization rules subpackage (zero DB/LLM/upper imports)."""

from .project_rules import evaluate_project_rules
from .script_rules import SINGLE_SCRIPT_RULE_CATALOG, evaluate_script_rules

__all__ = [
    "SINGLE_SCRIPT_RULE_CATALOG",
    "evaluate_project_rules",
    "evaluate_script_rules",
]
