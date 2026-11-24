"""Dialect-aware SQL / PL/SQL statement chunker with dynamic SQL unwrapping and exact line tracking."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class ChunkRecord:
    """Single executable SQL statement chunk extracted from a script."""

    chunk_index: int
    start_line: int
    end_line: int
    statement_kind: str
    is_dynamic_sql: bool
    control_context: str
    raw_sql: str
    effective_sql: str
    comments: List[str] = field(default_factory=list)


@dataclass
class ParsedScript:
    """Script-level structural parse result produced offline."""

    dialect: str
    file_kind: str  # DDL | PROCEDURE | QUERY | SCRIPT
    line_count: int
    has_exception_handler: bool
    commit_count: int
    rollback_count: int
    chunks: List[ChunkRecord] = field(default_factory=list)


_PROCEDURE_HEADER_RE = re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:EDITIONABLE\s+|NONEDITIONABLE\s+)?"
    r"(PROCEDURE|FUNCTION|TRIGGER|PACKAGE(?:\s+BODY)?)\b",
    re.IGNORECASE,
)
_DDL_HEADER_RE = re.compile(
    r"^\s*CREATE\s+(?:GLOBAL\s+TEMPORARY\s+|EXTERNAL\s+|UNIQUE\s+|BITMAP\s+|OR\s+REPLACE\s+)?"
    r"(TABLE|INDEX|VIEW|MATERIALIZED\s+VIEW|SEQUENCE|SYNONYM)\b",
    re.IGNORECASE,
)
_EXECUTE_IMMEDIATE_RE = re.compile(
    r"\bEXECUTE\s+IMMEDIATE\s+(?P<body>.+?)(?:\bINTO\b|\bUSING\b|;|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_SINGLE_QUOTED_RE = re.compile(r"'((?:''|[^'])*)'", re.DOTALL)
_EXCEPTION_RE = re.compile(r"\bEXCEPTION\b", re.IGNORECASE)
_COMMIT_RE = re.compile(r"\bCOMMIT\s*(?:WORK)?\s*;", re.IGNORECASE)
_ROLLBACK_RE = re.compile(r"\bROLLBACK\s*(?:WORK)?\s*;", re.IGNORECASE)


def detect_sql_dialect(text: str, filename: str = "") -> str:
    """Infer SQL dialect heuristically from filename and syntax markers."""
    lower_name = filename.lower()
    if lower_name.endswith((".pls", ".pkb", ".pks", ".prc", ".fnc")):
        return "oracle"
    if lower_name.endswith(".hql"):
        return "hive"
    upper = text.upper()
    if any(
        marker in upper
        for marker in (
            "VARCHAR2",
            "NVL(",
            "SYS_GUID(",
            "SYSDATE",
            "EXECUTE IMMEDIATE",
            "CREATE OR REPLACE PROCEDURE",
            "SEGMENT CREATION IMMEDIATE",
            "TABLESPACE ",
        )
    ):
        return "oracle"
    if any(marker in upper for marker in ("STORED AS PARQUET", "TBLPROPERTIES", "INSERT OVERWRITE TABLE")):
        return "hive"
    if any(marker in upper for marker in ("CREATE OR REPLACE TEMP VIEW", "USING DELTA")):
        return "spark"
    if any(marker in upper for marker in ("SERIAL", "RETURNING ", "::VARCHAR", "ILIKE ")):
        return "postgres"
    if any(marker in upper for marker in ("AUTO_INCREMENT", "ENGINE=INNODB")):
        return "mysql"
    return "oracle" if "PL/SQL" in upper else "generic"


def _strip_comments_preserve_newlines(sql: str) -> Tuple[str, List[Tuple[int, str]]]:
    """Replace comments with spaces while keeping newlines intact and collecting comments with line numbers."""
    out: List[str] = []
    comments: List[Tuple[int, str]] = []
    n = len(sql)
    i = 0
    line = 1
    in_sq = False
    in_dq = False

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if ch == "\n":
            line += 1
            out.append(ch)
            i += 1
            continue

        if not in_sq and not in_dq and ch == "-" and nxt == "-":
            start_line = line
            j = i + 2
            while j < n and sql[j] != "\n":
                j += 1
            text = sql[i:j].strip()
            if text:
                comments.append((start_line, text))
            out.append(" " * (j - i))
            i = j
            continue

        if not in_sq and not in_dq and ch == "/" and nxt == "*":
            start_line = line
            out.append("  ")
            j = i + 2
            while j < n:
                if j < n - 1 and sql[j] == "*" and sql[j + 1] == "/":
                    out.append("  ")
                    j += 2
                    break
                if sql[j] == "\n":
                    line += 1
                    out.append("\n")
                else:
                    out.append(" ")
                j += 1
            block_txt = sql[i:j].strip()
            if block_txt:
                comments.append((start_line, block_txt))
            i = j
            continue

        if ch == "'" and not in_dq:
            out.append(ch)
            if in_sq and nxt == "'":
                out.append(nxt)
                i += 2
                continue
            in_sq = not in_sq
            i += 1
            continue

        if ch == '"' and not in_sq:
            in_dq = not in_dq
            out.append(ch)
            i += 1
            continue

        out.append(ch)
        i += 1

    return "".join(out), comments


def _line_of_offset(sql: str, offset: int) -> int:
    return sql.count("\n", 0, max(0, min(offset, len(sql)))) + 1


def _trim_span(raw_text: str, start_idx: int, end_idx: int) -> Optional[Tuple[int, int]]:
    while start_idx < end_idx and raw_text[start_idx].isspace():
        start_idx += 1
    while end_idx > start_idx and raw_text[end_idx - 1].isspace():
        end_idx -= 1
    if start_idx >= end_idx:
        return None
    return start_idx, end_idx


def _unwrap_execute_immediate(stmt_text: str) -> Tuple[bool, str]:
    """Extract inner dynamic SQL string literals from `EXECUTE IMMEDIATE ...` statements."""
    m = _EXECUTE_IMMEDIATE_RE.search(stmt_text)
    if not m:
        return False, stmt_text
    expr = m.group("body").strip()
    # If expression is pure variable name, cannot expand statically cleanly
    parts = _SINGLE_QUOTED_RE.findall(expr)
    if not parts:
        return True, stmt_text
    # Rejoin concatenated string literals with a placeholder identifier for dynamic variables
    joined = " :DYN_VAR ".join(p.replace("''", "'") for p in parts if p.strip())
    joined = joined.strip()
    if not joined:
        return True, stmt_text
    return True, joined


def _classify_statement_kind(sql_text: str, is_dynamic: bool) -> str:
    upper = re.sub(r"/\*.*?\*/", " ", sql_text, flags=re.DOTALL)
    upper = re.sub(r"--[^\n]*", " ", upper).strip().upper()
    if not upper:
        return "OTHER"
    if upper.startswith("MERGE"):
        return "MERGE"
    if upper.startswith("INSERT"):
        return "INSERT"
    if upper.startswith("UPDATE"):
        return "UPDATE"
    if upper.startswith("DELETE"):
        return "DELETE"
    if upper.startswith("TRUNCATE"):
        return "TRUNCATE"
    if upper.startswith("CREATE"):
        if re.match(r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:GLOBAL\s+TEMPORARY\s+|EXTERNAL\s+)?TABLE\b.*?\bAS\s+SELECT\b", upper, re.DOTALL):
            return "CTAS"
        if re.match(r"CREATE\s+(?:GLOBAL\s+TEMPORARY\s+|EXTERNAL\s+)?TABLE\b", upper):
            return "CREATE_TABLE"
        if re.match(r"CREATE\s+(?:UNIQUE\s+|BITMAP\s+)?INDEX\b", upper):
            return "CREATE_INDEX"
        if re.match(r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:MATERIALIZED\s+)?VIEW\b", upper):
            return "CREATE_VIEW"
        return "DDL"
    if upper.startswith("ALTER"):
        return "ALTER"
    if upper.startswith("DROP"):
        return "DROP"
    if upper.startswith(("SELECT", "WITH")):
        return "SELECT"
    if upper.startswith("COMMIT"):
        return "COMMIT"
    if upper.startswith("ROLLBACK"):
        return "ROLLBACK"
    if upper.startswith(("CALL", "EXEC")):
        return "CALL"
    if is_dynamic:
        return "EXECUTE_IMMEDIATE"
    return "OTHER"


def _find_plsql_body_span(masked: str) -> Optional[Tuple[int, int]]:
    """Locate outer PL/SQL `BEGIN` ... `END [name];` span if present."""
    # Look for first standalone BEGIN after AS/IS or top-level DECLARE/BEGIN
    header_m = _PROCEDURE_HEADER_RE.search(masked)
    search_start = 0
    if header_m:
        as_is = re.search(r"\b(?:AS|IS)\b", masked[header_m.end():], re.IGNORECASE)
        if as_is:
            search_start = header_m.end() + as_is.end()
    # Find BEGIN token outside quotes (masked already blanked comments)
    begin_m = re.search(r"\bBEGIN\b", masked[search_start:], re.IGNORECASE)
    if not begin_m:
        return None
    begin_pos = search_start + begin_m.end()
    return begin_pos, len(masked)


def _split_plsql_body_statements(
    raw_sql: str,
    masked_sql: str,
    body_start: int,
    body_end: int,
) -> List[Tuple[int, int, str]]:
    """Scan PL/SQL body into executable SQL statements, tracking control-flow context."""
    tokens = list(re.finditer(r"[A-Za-z_][A-Za-z0-9_$#]*|;", masked_sql[body_start:body_end]))
    in_sq = False

    def _is_quoted(abs_pos: int) -> bool:
        return False

    # Build simple character mask for string literals inside body slice so regex tokens inside quotes are ignored
    in_quote_flags = [False] * (body_end - body_start)
    sq = False
    i = body_start
    while i < body_end:
        ch = masked_sql[i]
        nxt = masked_sql[i + 1] if i + 1 < body_end else ""
        if ch == "'":
            in_quote_flags[i - body_start] = True
            if sq and nxt == "'":
                if i + 1 < body_end:
                    in_quote_flags[i + 1 - body_start] = True
                i += 2
                continue
            sq = not sq
            i += 1
            continue
        in_quote_flags[i - body_start] = sq
        i += 1

    # Also ignore positions inside parens for top-level control statements (e.g. SELECT inside IF?)
    paren_depth_at: Dict[int, int] = {}
    depth = 0
    for idx in range(body_end - body_start):
        if in_quote_flags[idx]:
            paren_depth_at[idx] = depth
            continue
        c = masked_sql[body_start + idx]
        if c == "(":
            depth += 1
        elif c == ")":
            depth = max(0, depth - 1)
        paren_depth_at[idx] = depth

    clean_tokens: List[Tuple[str, int, int, int]] = []
    for m in tokens:
        rel_s, rel_e = m.span()
        if in_quote_flags[rel_s]:
            continue
        clean_tokens.append((m.group(0), rel_s + body_start, rel_e + body_start, paren_depth_at[rel_s]))

    stmt_spans: List[Tuple[int, int, str]] = []
    ctx_stack: List[str] = []
    block_depth = 1  # outer BEGIN already entered
    stmt_start = body_start
    in_exception = False
    waiting_if_then = False
    if_header_start = -1
    waiting_loop_kw = False
    loop_header_start = -1
    sql_case_depth = 0

    idx = 0
    while idx < len(clean_tokens):
        tok, abs_s, abs_e, pdepth = clean_tokens[idx]
        upper = tok.upper()

        if pdepth == 0:
            at_boundary = not masked_sql[stmt_start:abs_s].strip()

            if upper == "CASE":
                prev_u = clean_tokens[idx - 1][0].upper() if idx > 0 else ""
                if prev_u != "END":
                    sql_case_depth += 1
                    idx += 1
                    continue

            if sql_case_depth > 0:
                if upper == "END":
                    sql_case_depth -= 1
                    nxt_tok = clean_tokens[idx + 1][0].upper() if idx + 1 < len(clean_tokens) else ""
                    if nxt_tok == "CASE" and at_boundary:
                        j = idx + 2
                        while j < len(clean_tokens) and clean_tokens[j][0] != ";":
                            j += 1
                        stmt_start = clean_tokens[j][2] if j < len(clean_tokens) else abs_e
                        idx = j + 1
                        continue
                elif tok == ";":
                    sql_case_depth = 0
                if tok != ";":
                    idx += 1
                    continue

            if upper == "EXCEPTION" and not in_exception and at_boundary:
                in_exception = True
                ctx_stack.append("EXCEPTION")
                stmt_start = abs_e
                idx += 1
                continue

            if upper in ("IF", "ELSIF") and (at_boundary or upper == "ELSIF"):
                # Close previous IF header label only on END IF, wait for THEN
                if upper == "ELSIF" and ctx_stack and ctx_stack[-1].startswith("IF"):
                    ctx_stack.pop()
                if at_boundary:
                    waiting_if_then = True
                    if_header_start = abs_s
                    idx += 1
                    continue

            if upper == "THEN" and waiting_if_then:
                waiting_if_then = False
                cond_snippet = re.sub(r"\s+", " ", masked_sql[if_header_start:abs_s]).strip()
                if len(cond_snippet) > 96:
                    cond_snippet = cond_snippet[:93] + "..."
                ctx_stack.append(cond_snippet)
                stmt_start = abs_e
                idx += 1
                continue

            if upper in ("FOR", "WHILE") and at_boundary:
                waiting_loop_kw = True
                loop_header_start = abs_s
                idx += 1
                continue

            if upper == "LOOP" and (waiting_loop_kw or at_boundary):
                if waiting_loop_kw:
                    waiting_loop_kw = False
                    loop_snippet = re.sub(r"\s+", " ", masked_sql[loop_header_start:abs_e]).strip()
                    if len(loop_snippet) > 96:
                        loop_snippet = loop_snippet[:93] + "..."
                    ctx_stack.append(loop_snippet)
                elif idx > 0 and clean_tokens[idx - 1][0].upper() == "END":
                    # Handled by END lookahead below
                    pass
                else:
                    ctx_stack.append("LOOP")
                stmt_start = abs_e
                idx += 1
                continue

            if upper == "BEGIN" and at_boundary:
                block_depth += 1
                ctx_stack.append("BEGIN")
                stmt_start = abs_e
                idx += 1
                continue

            if upper == "ELSE" and at_boundary and ctx_stack and ctx_stack[-1].startswith(("IF", "ELSIF")):
                ctx_stack[-1] = ctx_stack[-1] + " / ELSE"
                stmt_start = abs_e
                idx += 1
                continue

            if upper == "END":
                nxt_tok = clean_tokens[idx + 1][0].upper() if idx + 1 < len(clean_tokens) else ""
                if nxt_tok == "IF":
                    if ctx_stack and ctx_stack[-1].startswith(("IF", "ELSIF")):
                        ctx_stack.pop()
                    # advance past END IF ;
                    j = idx + 2
                    while j < len(clean_tokens) and clean_tokens[j][0] != ";":
                        j += 1
                    stmt_start = clean_tokens[j][2] if j < len(clean_tokens) else abs_e
                    idx = j + 1
                    continue
                if nxt_tok == "LOOP":
                    if ctx_stack and ("LOOP" in ctx_stack[-1] or ctx_stack[-1].startswith(("FOR", "WHILE"))):
                        ctx_stack.pop()
                    j = idx + 2
                    while j < len(clean_tokens) and clean_tokens[j][0] != ";":
                        j += 1
                    stmt_start = clean_tokens[j][2] if j < len(clean_tokens) else abs_e
                    idx = j + 1
                    continue
                if nxt_tok == "CASE":
                    # Could be SQL CASE expression in statement; ignore if inside statement unless top-level PL/SQL END CASE;
                    stmt_text_so_far = masked_sql[stmt_start:abs_s].strip().upper()
                    if not stmt_text_so_far:
                        j = idx + 2
                        while j < len(clean_tokens) and clean_tokens[j][0] != ";":
                            j += 1
                        stmt_start = clean_tokens[j][2] if j < len(clean_tokens) else abs_e
                        idx = j + 1
                        continue
                # Might be closing outer or inner BEGIN block if stmt_text_so_far is empty or starts with whitespace
                stmt_so_far = masked_sql[stmt_start:abs_s].strip()
                if not stmt_so_far:
                    block_depth -= 1
                    if ctx_stack and ctx_stack[-1] == "BEGIN":
                        ctx_stack.pop()
                    j = idx + 1
                    while j < len(clean_tokens) and clean_tokens[j][0] != ";":
                        j += 1
                    stmt_start = clean_tokens[j][2] if j < len(clean_tokens) else abs_e
                    idx = j + 1
                    if block_depth <= 0:
                        break
                    continue

            if tok == ";":
                trimmed = _trim_span(raw_sql, stmt_start, abs_e)
                if trimmed:
                    s_i, e_i = trimmed
                    # Skip pure PL/SQL variable declarations or END IF wrappers or ELSE prefixes
                    masked_piece = masked_sql[s_i:e_i].strip()
                    if masked_piece and not re.match(r"^(?:END(?:\s+(?:IF|LOOP|CASE))?|NULL)\s*;\s*$", masked_piece, re.IGNORECASE):
                        ctx = " > ".join(c for c in ctx_stack if c != "BEGIN") or "MAIN"
                        stmt_spans.append((s_i, e_i, ctx))
                stmt_start = abs_e
        idx += 1

    return stmt_spans


def _split_generic_statements(raw_sql: str, masked_sql: str) -> List[Tuple[int, int, str]]:
    """Split standalone SQL script by semicolons outside quotes and parentheses."""
    spans: List[Tuple[int, int, str]] = []
    start = 0
    in_sq = False
    in_dq = False
    paren = 0
    n = len(masked_sql)
    i = 0
    while i < n:
        ch = masked_sql[i]
        nxt = masked_sql[i + 1] if i + 1 < n else ""
        if ch == "'" and not in_dq:
            if in_sq and nxt == "'":
                i += 2
                continue
            in_sq = not in_sq
            i += 1
            continue
        if ch == '"' and not in_sq:
            in_dq = not in_dq
            i += 1
            continue
        if not in_sq and not in_dq:
            if ch == "(":
                paren += 1
            elif ch == ")":
                paren = max(0, paren - 1)
            elif ch == ";" and paren == 0:
                trimmed = _trim_span(raw_sql, start, i + 1)
                if trimmed:
                    s_i, e_i = trimmed
                    if masked_sql[s_i:e_i].strip() not in ("", "/", ";"):
                        spans.append((s_i, e_i, "TOP_LEVEL"))
                start = i + 1
        i += 1
    trailing = _trim_span(raw_sql, start, n)
    if trailing:
        s_i, e_i = trailing
        piece = masked_sql[s_i:e_i].strip()
        if piece and piece not in ("/", ";"):
            spans.append((s_i, e_i, "TOP_LEVEL"))
    return spans


def chunk_sql_script(
    raw_sql: str,
    *,
    filename: str = "",
    dialect: Optional[str] = None,
) -> ParsedScript:
    """Parse a SQL / PL/SQL script into ordered, line-accurate `ChunkRecord` blocks."""
    detected_dialect = dialect or detect_sql_dialect(raw_sql, filename=filename)
    line_count = raw_sql.count("\n") + (1 if raw_sql else 0)
    masked, comments = _strip_comments_preserve_newlines(raw_sql)

    has_exception = bool(_EXCEPTION_RE.search(masked))
    commit_count = len(_COMMIT_RE.findall(masked))
    rollback_count = len(_ROLLBACK_RE.findall(masked))

    is_procedure = bool(_PROCEDURE_HEADER_RE.search(masked))
    plsql_body = _find_plsql_body_span(masked)

    if is_procedure and plsql_body is not None:
        file_kind = "PROCEDURE"
        spans = _split_plsql_body_statements(raw_sql, masked, plsql_body[0], plsql_body[1])
        if not spans:
            spans = _split_generic_statements(raw_sql, masked)
    else:
        spans = _split_generic_statements(raw_sql, masked)
        ddl_hits = sum(1 for s_i, e_i, _ in spans if _DDL_HEADER_RE.match(masked[s_i:e_i]))
        if spans and ddl_hits >= max(1, len(spans) // 2):
            file_kind = "DDL"
        elif any(
            _classify_statement_kind(masked[s_i:e_i], False) in ("INSERT", "MERGE", "UPDATE", "DELETE", "CTAS")
            for s_i, e_i, _ in spans
        ):
            file_kind = "SCRIPT"
        else:
            file_kind = "QUERY" if spans else "SCRIPT"

    chunks: List[ChunkRecord] = []
    for idx, (s_i, e_i, ctx) in enumerate(spans):
        chunk_text = raw_sql[s_i:e_i]
        s_line = _line_of_offset(raw_sql, s_i)
        e_line = _line_of_offset(raw_sql, max(s_i, e_i - 1))
        is_dynamic, effective_sql = _unwrap_execute_immediate(chunk_text)
        kind = _classify_statement_kind(effective_sql, is_dynamic)
        # Skip noise lines inside PL/SQL (e.g. V_CNT := 10; or SQLCODE/DBMS_OUTPUT/PRAGMA) unless SQL DML/DDL
        masked_chunk = masked[s_i:e_i].strip()
        if is_procedure and kind == "OTHER":
            # Keep RETURN/OPEN/EXECUTE/assignment only if it has DML or SELECT
            if not re.search(r"\b(SELECT|INSERT|UPDATE|DELETE|MERGE|TRUNCATE|EXECUTE\s+IMMEDIATE|COMMIT|ROLLBACK)\b", masked_chunk, re.IGNORECASE):
                continue

        chunk_comments = [
            c_txt
            for c_line, c_txt in comments
            if max(1, s_line - 2) <= c_line <= e_line
        ]
        chunks.append(
            ChunkRecord(
                chunk_index=len(chunks),
                start_line=s_line,
                end_line=e_line,
                statement_kind=kind,
                is_dynamic_sql=is_dynamic,
                control_context=ctx,
                raw_sql=chunk_text,
                effective_sql=effective_sql if is_dynamic else chunk_text,
                comments=chunk_comments,
            )
        )

    return ParsedScript(
        dialect=detected_dialect,
        file_kind=file_kind,
        line_count=line_count,
        has_exception_handler=has_exception,
        commit_count=commit_count,
        rollback_count=rollback_count,
        chunks=chunks,
    )
