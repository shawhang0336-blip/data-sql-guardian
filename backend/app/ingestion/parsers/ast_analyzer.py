"""Pure-offline SQL AST feature extractor & cross-script structural fingerprint generator."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    import sqlglot
    from sqlglot import exp as sg_exp
except Exception:  # pragma: no cover - safe fallback if sqlglot missing
    sqlglot = None
    sg_exp = None


@dataclass
class JoinCondition:
    left_expr: str
    right_expr: str
    left_col: str
    right_col: str
    left_func_wrapped: bool
    right_func_wrapped: bool
    raw: str


@dataclass
class AstFeatures:
    statement_kind: str
    target_tables: List[str] = field(default_factory=list)
    source_tables: List[str] = field(default_factory=list)
    cte_names: List[str] = field(default_factory=list)
    join_conditions: List[Dict[str, Any]] = field(default_factory=list)
    where_func_columns: List[str] = field(default_factory=list)
    null_checked_columns: List[str] = field(default_factory=list)
    subquery_equality_refs: List[Dict[str, str]] = field(default_factory=list)
    dead_null_join_columns: List[str] = field(default_factory=list)
    has_not_in: bool = False
    not_in_subquery: bool = False
    leading_wildcard_likes: List[str] = field(default_factory=list)
    has_select_star: bool = False
    has_distinct: bool = False
    has_union_without_all: bool = False
    has_nondeterministic_key: bool = False
    has_sysdate_branch: bool = False
    has_or_across_columns: bool = False
    has_implicit_cross_join: bool = False
    has_missing_where_mutation: bool = False
    has_insert_without_columns: bool = False
    has_truncate: bool = False
    has_commit: bool = False
    has_ddl_drop_without_if: bool = False
    ddl_no_primary_key: bool = False
    select_into_count: int = 0
    ast_fingerprint: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


_SQL_FUNC_NAMES = {
    "TRIM",
    "LTRIM",
    "RTRIM",
    "UPPER",
    "LOWER",
    "SUBSTR",
    "SUBSTRING",
    "REPLACE",
    "TO_CHAR",
    "TO_DATE",
    "TO_NUMBER",
    "NVL",
    "NVL2",
    "COALESCE",
    "DECODE",
    "CAST",
    "ROUND",
    "TRUNC",
    "INSTR",
    "REGEXP_REPLACE",
    "LPAD",
    "RPAD",
}

_IDENT_CHARS = r'[A-Za-z_"][A-Za-z0-9_$#"]*'
_QUAL_IDENT = rf"(?:{_IDENT_CHARS}\.)?{_IDENT_CHARS}"


def normalize_identifier(raw: str) -> str:
    """Normalize schema/table/column identifiers by removing quotes and uppercasing."""
    text = raw.strip().strip(";").strip()
    if not text:
        return ""
    parts = [p.strip().strip('"').strip("`").upper() for p in text.split(".") if p.strip()]
    if not parts:
        return ""
    # Drop common Oracle schema prefixes so `SACCNUA1.FOO` aligns with `FOO`
    if len(parts) >= 2 and parts[0] not in ("DUAL",):
        return parts[-1]
    return parts[-1]


def _normalize_col_ref(expr: str) -> Tuple[str, str]:
    """Return (qualified_upper, bare_upper) if expr contains a simple column ref."""
    cleaned = re.sub(r"\s+", "", expr.strip().strip('"').upper())
    # Look inside simple function call like TRIM(A.COL) or SUBSTR(A.COL, 1, 3)
    col_matches = re.findall(r"([A-Z_][A-Z0-9_$#]*(?:\.[A-Z_][A-Z0-9_$#]*)?)", cleaned)
    for tok in col_matches:
        head = tok.split(".")[0]
        if head in _SQL_FUNC_NAMES or tok.isdigit():
            continue
        if tok in ("AND", "OR", "NOT", "NULL", "TRUE", "FALSE", "SYSDATE", "DUAL"):
            continue
        bare = tok.split(".")[-1]
        return tok, bare
    return "", ""


def _strip_comments_and_strings(sql: str) -> str:
    no_block = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    no_line = re.sub(r"--[^\n]*", " ", no_block)
    return no_line


def _mask_string_literals(sql: str) -> str:
    return re.sub(r"'(?:''|[^'])*'", "'?'", sql, flags=re.DOTALL)


def compute_normalized_fingerprint(sql: str) -> str:
    """Generate structurally stable AST/token fingerprint across formatting/comment/literal differences."""
    cleaned = _strip_comments_and_strings(sql)
    cleaned = _mask_string_literals(cleaned)
    cleaned = re.sub(r"\b\d+(?:\.\d+)?\b", "0", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().upper().rstrip(";")
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:16]


def _dialect_for_sqlglot(dialect: str) -> Optional[str]:
    mapping = {
        "oracle": "oracle",
        "hive": "hive",
        "spark": "spark",
        "postgres": "postgres",
        "mysql": "mysql",
        "generic": None,
    }
    return mapping.get(dialect, None)


def _extract_tables_and_ctes_regex(sql_clean: str, stmt_kind: str) -> Tuple[List[str], List[str], List[str]]:
    ctes: Set[str] = set()
    targets: List[str] = []
    sources: List[str] = []

    for m in re.finditer(r"\b(?:WITH|,)\s*([A-Za-z_][A-Za-z0-9_$#]*)\s+AS\s*\(", sql_clean, re.IGNORECASE):
        ctes.add(m.group(1).upper())

    def _add_target(raw: str) -> None:
        name = normalize_identifier(raw)
        if name and name not in ("DUAL", "TABLE") and name not in targets:
            targets.append(name)

    def _add_source(raw: str) -> None:
        name = normalize_identifier(raw)
        if not name or name in ("DUAL", "SELECT", "WHERE", "SET", "VALUES", "TABLE"):
            return
        if name in ctes:
            return
        if name not in sources and name not in targets:
            sources.append(name)
        elif name not in sources and stmt_kind in ("UPDATE", "MERGE", "DELETE"):
            # Self-read allowed
            pass

    for m in re.finditer(
        rf"\bINSERT\s+(?:/\*\+.*?\*/\s+)?(?:INTO|OVERWRITE\s+TABLE)\s+({_QUAL_IDENT})",
        sql_clean,
        re.IGNORECASE | re.DOTALL,
    ):
        _add_target(m.group(1))

    for m in re.finditer(rf"\bMERGE\s+INTO\s+({_QUAL_IDENT})", sql_clean, re.IGNORECASE):
        _add_target(m.group(1))

    for m in re.finditer(rf"\bUPDATE\s+(?:/\*\+.*?\*/\s+)?({_QUAL_IDENT})\b", sql_clean, re.IGNORECASE):
        cand = normalize_identifier(m.group(1))
        if cand not in ("SET",):
            _add_target(cand)

    for m in re.finditer(rf"\bDELETE\s+(?:FROM\s+)?({_QUAL_IDENT})\b", sql_clean, re.IGNORECASE):
        cand = normalize_identifier(m.group(1))
        if cand not in ("FROM", "WHERE"):
            _add_target(cand)

    for m in re.finditer(rf"\bTRUNCATE\s+TABLE\s+({_QUAL_IDENT})", sql_clean, re.IGNORECASE):
        _add_target(m.group(1))

    for m in re.finditer(
        rf"\bCREATE\s+(?:GLOBAL\s+TEMPORARY\s+|EXTERNAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?({_QUAL_IDENT})",
        sql_clean,
        re.IGNORECASE,
    ):
        _add_target(m.group(1))

    for m in re.finditer(rf"\b(?:FROM|JOIN|USING)\s+({_QUAL_IDENT})(?:\s+(?:AS\s+)?[A-Za-z_][A-Za-z0-9_$#]*)?", sql_clean, re.IGNORECASE):
        tok = m.group(1).strip()
        if tok.startswith("("):
            continue
        cand = normalize_identifier(tok)
        if cand in ("SELECT", "VALUES", "DUAL", "TABLE", "THE", "SET"):
            continue
        if cand in ctes or cand in targets:
            continue
        _add_source(cand)

    # Comma joins in FROM A, B, C
    for from_m in re.finditer(r"\bFROM\s+([^;()]+?)(?:\bWHERE\b|\bGROUP\b|\bORDER\b|\bHAVING\b|\bUNION\b|\bLEFT\b|\bRIGHT\b|\bINNER\b|\bJOIN\b|\bON\b|;|\Z)", sql_clean, re.IGNORECASE | re.DOTALL):
        seg = from_m.group(1)
        if "," in seg:
            for part in seg.split(","):
                first_tok = part.strip().split()[0] if part.strip() else ""
                if first_tok and re.match(rf"^{_QUAL_IDENT}$", first_tok):
                    cand = normalize_identifier(first_tok)
                    if cand not in ctes and cand not in ("DUAL", "SELECT") and cand not in sources:
                        sources.append(cand)

    return targets, sources, sorted(ctes)


def _extract_tables_sqlglot(sql_clean: str, dialect: str, stmt_kind: str) -> Optional[Tuple[List[str], List[str], List[str]]]:
    if sqlglot is None:
        return None
    try:
        parsed = sqlglot.parse_one(sql_clean, read=_dialect_for_sqlglot(dialect))
    except Exception:
        return None
    ctes = {cte.alias_or_name.upper() for cte in parsed.find_all(sg_exp.CTE) if cte.alias_or_name}
    targets: List[str] = []
    sources: List[str] = []

    target_nodes: Set[int] = set()
    for node in parsed.find_all((sg_exp.Insert, sg_exp.Merge, sg_exp.Update, sg_exp.Delete, sg_exp.Create)):
        tbl = node.find(sg_exp.Table)
        if tbl is not None:
            name = normalize_identifier(tbl.name)
            if name and name not in targets:
                targets.append(name)
            target_nodes.add(id(tbl))

    for tbl in parsed.find_all(sg_exp.Table):
        if id(tbl) in target_nodes:
            continue
        name = normalize_identifier(tbl.name)
        if not name or name in ctes or name == "DUAL":
            continue
        if name not in sources and name not in targets:
            sources.append(name)

    return targets, sources, sorted(ctes)


def _is_func_wrapped(expr: str) -> bool:
    return bool(re.search(r"\b(" + "|".join(sorted(_SQL_FUNC_NAMES)) + r")\s*\(", expr.upper()))


def _extract_join_and_where_features(sql_clean: str) -> Dict[str, Any]:
    join_conditions: List[Dict[str, Any]] = []
    where_func_cols: List[str] = []
    null_checked: List[str] = []
    subquery_eq_refs: List[Dict[str, str]] = []
    dead_null_cols: List[str] = []

    # 1. Analyze JOIN ... ON ... conditions and equality expressions like FUNC(A.X) = B.Y
    on_blocks = re.finditer(
        r"\bON\s+(.+?)(?=\b(?:LEFT|RIGHT|INNER|FULL|CROSS)?\s*JOIN\b|\bWHERE\b|\bGROUP\b|\bORDER\b|\bWHEN\b|\bSET\b|;|\Z)",
        sql_clean,
        re.IGNORECASE | re.DOTALL,
    )
    eq_pattern = re.compile(
        r"([A-Za-z_][A-Za-z0-9_$#.\(\),\s'/*+-]*?)\s*=\s*([A-Za-z_][A-Za-z0-9_$#.\(\),\s'/*+-]*?)(?=\bAND\b|\bOR\b|\)|\Z)",
        re.IGNORECASE,
    )
    for ob in on_blocks:
        clause = ob.group(1)
        for eq in eq_pattern.finditer(clause):
            left = eq.group(1).strip()
            right = eq.group(2).strip()
            if not left or not right:
                continue
            l_q, l_b = _normalize_col_ref(left)
            r_q, r_b = _normalize_col_ref(right)
            if not l_b or not r_b:
                continue
            l_fn = _is_func_wrapped(left)
            r_fn = _is_func_wrapped(right)
            join_conditions.append(
                {
                    "left_expr": left[:80],
                    "right_expr": right[:80],
                    "left_col": l_q or l_b,
                    "right_col": r_q or r_b,
                    "left_func_wrapped": l_fn,
                    "right_func_wrapped": r_fn,
                    "raw": f"{left[:60]} = {right[:60]}",
                }
            )

    # 2. Function calls on columns compared in WHERE (e.g., TO_CHAR(A.DT, '...') = ... or TRIM(A.COL) = ...)
    func_cmp_re = re.compile(
        r"\b(" + "|".join(sorted(_SQL_FUNC_NAMES)) + r")\s*\(\s*([A-Za-z_][A-Za-z0-9_$#]*(?:\.[A-Za-z_][A-Za-z0-9_$#]*)?)"
        r"[^()]*\)\s*(?:=|!=|<>|>=|<=|>|<|\bIN\b|\bBETWEEN\b|\bLIKE\b)",
        re.IGNORECASE,
    )
    for m in func_cmp_re.finditer(sql_clean):
        col = m.group(2).upper()
        if col not in ("SYSDATE", "DUAL", "NULL") and col not in where_func_cols:
            where_func_cols.append(col)

    # 3. Columns explicitly constrained with IS NULL
    null_re = re.compile(r"\b([A-Za-z_][A-Za-z0-9_$#]*(?:\.[A-Za-z_][A-Za-z0-9_$#]*)?)\s+IS\s+NULL\b", re.IGNORECASE)
    for m in null_re.finditer(sql_clean):
        col_q = m.group(1).upper()
        if col_q not in null_checked:
            null_checked.append(col_q)

    # 4. Equality references inside EXISTS / NOT EXISTS / IN subqueries referencing an IS NULL column
    subq_re = re.compile(r"\b(?:NOT\s+)?(?:EXISTS|IN)\s*\(\s*(SELECT\b.+?\))", re.IGNORECASE | re.DOTALL)
    # Use balanced paren finder for EXISTS / NOT EXISTS blocks to catch multi-condition subqueries
    for m in re.finditer(r"\b(?:NOT\s+)?EXISTS\s*\(", sql_clean, re.IGNORECASE):
        pos = m.end()
        depth = 1
        end = pos
        while end < len(sql_clean) and depth > 0:
            if sql_clean[end] == "(":
                depth += 1
            elif sql_clean[end] == ")":
                depth -= 1
            end += 1
        sub_body = sql_clean[pos : max(pos, end - 1)]
        for eq in re.finditer(
            r"\b([A-Za-z_][A-Za-z0-9_$#]*\.[A-Za-z_][A-Za-z0-9_$#]*)\s*=\s*([A-Za-z_][A-Za-z0-9_$#]*\.[A-Za-z_][A-Za-z0-9_$#]*)\b",
            sub_body,
        ):
            lhs = eq.group(1).upper()
            rhs = eq.group(2).upper()
            subquery_eq_refs.append({"lhs": lhs, "rhs": rhs})
            if lhs in null_checked and lhs not in dead_null_cols:
                dead_null_cols.append(lhs)
            if rhs in null_checked and rhs not in dead_null_cols:
                dead_null_cols.append(rhs)

    # Also check outer WHERE `col IS NULL ... AND T.COL = ...`
    for nc in null_checked:
        for jc in join_conditions:
            if nc == jc["left_col"] or nc == jc["right_col"]:
                if nc not in dead_null_cols:
                    dead_null_cols.append(nc)

    return {
        "join_conditions": join_conditions,
        "where_func_columns": where_func_cols,
        "null_checked_columns": null_checked,
        "subquery_equality_refs": subquery_eq_refs,
        "dead_null_join_columns": dead_null_cols,
    }


def analyze_chunk_ast(
    sql_text: str,
    *,
    statement_kind: str = "OTHER",
    dialect: str = "oracle",
) -> AstFeatures:
    """Analyze a single SQL statement chunk into structured `AstFeatures` without DB/LLM calls."""
    clean = _strip_comments_and_strings(sql_text)
    masked = _mask_string_literals(clean)
    upper = masked.upper()

    extracted = _extract_tables_sqlglot(clean, dialect, statement_kind) if statement_kind in ("SELECT", "INSERT", "UPDATE", "DELETE", "MERGE", "CTAS") else None
    reg_targets, reg_sources, reg_ctes = _extract_tables_and_ctes_regex(clean, statement_kind)

    if extracted is not None:
        sg_targets, sg_sources, sg_ctes = extracted
        targets = list(dict.fromkeys(sg_targets + reg_targets))
        sources = list(dict.fromkeys(sg_sources + [s for s in reg_sources if s not in targets]))
        ctes = list(dict.fromkeys(sg_ctes + reg_ctes))
    else:
        targets, sources, ctes = reg_targets, reg_sources, reg_ctes

    rel_features = _extract_join_and_where_features(clean)

    # Leading wildcard LIKE '%...'
    leading_likes: List[str] = []
    for m in re.finditer(r"([A-Za-z_][A-Za-z0-9_$#.\(\)]*)\s+LIKE\s+'%[^']*'", clean, re.IGNORECASE):
        expr = f"{m.group(1)} LIKE '%...'"
        if expr not in leading_likes:
            leading_likes.append(expr)

    # NOT IN detection (subquery or list that could fail on NULL)
    has_not_in = bool(re.search(r"\bNOT\s+IN\s*\(", upper))
    not_in_subquery = bool(re.search(r"\bNOT\s+IN\s*\(\s*SELECT\b", upper))

    # SELECT * (excluding COUNT(*))
    has_select_star = bool(re.search(r"\bSELECT\s+(?:DISTINCT\s+)?(?:\*|[A-Za-z_][A-Za-z0-9_$#]*\.\*)", upper))

    # SELECT DISTINCT
    has_distinct = bool(re.search(r"\bSELECT\s+DISTINCT\b", upper))

    # UNION without ALL
    has_union_without_all = bool(re.search(r"\bUNION\b(?!\s+ALL\b)", upper))

    # Nondeterministic key generator inside INSERT/MERGE
    has_nondeterministic = statement_kind in ("INSERT", "MERGE", "CTAS") and bool(
        re.search(r"\b(?:SYS_GUID|UUID|RANDOM|NEWID|DBMS_RANDOM\.\w+)\s*\(", upper)
    )

    # Non-deterministic time branching
    has_sysdate_branch = bool(re.search(r"\b(?:SYSDATE|CURRENT_TIMESTAMP|SYSTIMESTAMP)\b", upper)) and bool(
        re.search(r"\b(?:IF|ELSIF|CASE|WHERE)\b", upper)
    )

    # OR across distinct columns in WHERE
    has_or_across_columns = bool(
        re.search(
            r"\b([A-Za-z_][A-Za-z0-9_$#]*\.[A-Za-z_][A-Za-z0-9_$#]*)\s*=\s*[^()]+\s+OR\s+([A-Za-z_][A-Za-z0-9_$#]*\.[A-Za-z_][A-Za-z0-9_$#]*)\s*=",
            upper,
        )
    )

    # Implicit cross join (`FROM T1, T2` with no ON/WHERE or explicit CROSS JOIN)
    has_implicit_cross = bool(re.search(r"\bCROSS\s+JOIN\b", upper))
    if not has_implicit_cross:
        from_comma = re.search(r"\bFROM\s+[A-Za-z_][A-Za-z0-9_$#.]*(?:\s+[A-Za-z_][A-Za-z0-9_$#]*)?\s*,\s*[A-Za-z_][A-Za-z0-9_$#.]*", upper)
        if from_comma and "WHERE" not in upper:
            has_implicit_cross = True

    # UPDATE / DELETE without WHERE
    has_missing_where_mutation = False
    if statement_kind == "UPDATE" and not re.search(r"\bWHERE\b", upper):
        has_missing_where_mutation = True
    elif statement_kind == "DELETE" and not re.search(r"\bWHERE\b", upper):
        has_missing_where_mutation = True

    # INSERT INTO TBL SELECT ... without explicit target column list `(col1, ...)`
    has_insert_without_cols = False
    if statement_kind == "INSERT":
        ins_m = re.search(
            rf"\bINSERT\s+(?:/\*\+.*?\*/\s+)?INTO\s+{_QUAL_IDENT}\s*(?P<after>.+?)(?:\bSELECT\b|\bWITH\b|\bVALUES\b)",
            clean,
            re.IGNORECASE | re.DOTALL,
        )
        if ins_m:
            after = ins_m.group("after").strip()
            if not after.startswith("("):
                has_insert_without_cols = True
        else:
            if re.search(rf"\bINSERT\s+(?:/\*\+.*?\*/\s+)?INTO\s+{_QUAL_IDENT}\s+(?:SELECT|WITH)\b", clean, re.IGNORECASE):
                has_insert_without_cols = True

    ddl_no_pk = False
    if statement_kind == "CREATE_TABLE":
        if not re.search(r"\bPRIMARY\s+KEY\b", upper):
            ddl_no_pk = True

    has_ddl_drop_without_if = statement_kind == "DROP" and not re.search(r"\bIF\s+EXISTS\b", upper)

    return AstFeatures(
        statement_kind=statement_kind,
        target_tables=targets,
        source_tables=sources,
        cte_names=ctes,
        join_conditions=rel_features["join_conditions"],
        where_func_columns=rel_features["where_func_columns"],
        null_checked_columns=rel_features["null_checked_columns"],
        subquery_equality_refs=rel_features["subquery_equality_refs"],
        dead_null_join_columns=rel_features["dead_null_join_columns"],
        has_not_in=has_not_in,
        not_in_subquery=not_in_subquery,
        leading_wildcard_likes=leading_likes,
        has_select_star=has_select_star,
        has_distinct=has_distinct,
        has_union_without_all=has_union_without_all,
        has_nondeterministic_key=has_nondeterministic,
        has_sysdate_branch=has_sysdate_branch,
        has_or_across_columns=has_or_across_columns,
        has_implicit_cross_join=has_implicit_cross,
        has_missing_where_mutation=has_missing_where_mutation,
        has_insert_without_columns=has_insert_without_cols,
        has_truncate=(statement_kind == "TRUNCATE"),
        has_commit=(statement_kind == "COMMIT"),
        has_ddl_drop_without_if=has_ddl_drop_without_if,
        ddl_no_primary_key=ddl_no_pk,
        ast_fingerprint=compute_normalized_fingerprint(sql_text),
    )


def aggregate_script_features(chunk_features: List[AstFeatures]) -> Dict[str, Any]:
    """Summarize target/source tables and composite fingerprint across all chunks of a script."""
    targets: List[str] = []
    sources: List[str] = []
    chunk_fps: List[str] = []
    for f in chunk_features:
        for t in f.target_tables:
            if t not in targets:
                targets.append(t)
        for s in f.source_tables:
            if s not in sources:
                sources.append(s)
        if f.ast_fingerprint:
            chunk_fps.append(f.ast_fingerprint)
    combined = "|".join(chunk_fps) if chunk_fps else "EMPTY"
    script_fp = hashlib.sha256(combined.encode("utf-8")).hexdigest()[:16]
    return {
        "target_tables": targets,
        "source_tables": sources,
        "ast_fingerprint": script_fp,
    }
