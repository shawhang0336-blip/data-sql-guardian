"""Pure-offline DDL parser supporting Oracle BYTE/CHAR + STORAGE clauses and ANSI/Hive/PG DDL."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class TableSchemaRecord:
    """Normalized table schema parsed from DDL statements."""

    table_name: str
    schema_name: str = ""
    columns: List[Dict[str, object]] = field(default_factory=list)
    primary_keys: List[str] = field(default_factory=list)
    indexes: List[Dict[str, object]] = field(default_factory=list)


_CREATE_TABLE_RE = re.compile(
    r"\bCREATE\s+(?:GLOBAL\s+TEMPORARY\s+|EXTERNAL\s+|OR\s+REPLACE\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?:(?P<schema>[A-Za-z_\"`][A-Za-z0-9_$#\"`]*)\.)?(?P<table>[A-Za-z_\"`][A-Za-z0-9_$#\"`]*)\s*\(",
    re.IGNORECASE,
)
_ALTER_PK_RE = re.compile(
    r"\bALTER\s+TABLE\s+(?:(?P<schema>[A-Za-z_\"`][A-Za-z0-9_$#\"`]*)\.)?(?P<table>[A-Za-z_\"`][A-Za-z0-9_$#\"`]*)"
    r".*?\bADD\s+(?:CONSTRAINT\s+\S+\s+)?PRIMARY\s+KEY\s*\((?P<cols>[^)]+)\)",
    re.IGNORECASE | re.DOTALL,
)
_CREATE_INDEX_RE = re.compile(
    r"\bCREATE\s+(?P<unique>UNIQUE\s+)?(?:BITMAP\s+)?INDEX\s+"
    r"(?:(?P<idx_schema>[A-Za-z_\"`][A-Za-z0-9_$#\"`]*)\.)?(?P<idx_name>[A-Za-z_\"`][A-Za-z0-9_$#\"`]*)\s+ON\s+"
    r"(?:(?P<tbl_schema>[A-Za-z_\"`][A-Za-z0-9_$#\"`]*)\.)?(?P<tbl>[A-Za-z_\"`][A-Za-z0-9_$#\"`]*)\s*\((?P<cols>[^)]+)\)",
    re.IGNORECASE | re.DOTALL,
)


def _unquote(ident: str) -> str:
    return ident.strip().strip('"').strip("`").upper()


def _extract_balanced_parens(text: str, open_paren_pos: int) -> str:
    """Return content inside parentheses starting just BEFORE or AT `(` at `open_paren_pos`."""
    idx = text.find("(", open_paren_pos)
    if idx < 0:
        return ""
    depth = 0
    in_sq = False
    n = len(text)
    i = idx
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "'":
            if in_sq and nxt == "'":
                i += 2
                continue
            in_sq = not in_sq
            i += 1
            continue
        if not in_sq:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return text[idx + 1 : i]
        i += 1
    return ""


def _split_top_level_commas(body: str) -> List[str]:
    items: List[str] = []
    start = 0
    depth = 0
    in_sq = False
    n = len(body)
    i = 0
    while i < n:
        ch = body[i]
        nxt = body[i + 1] if i + 1 < n else ""
        if ch == "'":
            if in_sq and nxt == "'":
                i += 2
                continue
            in_sq = not in_sq
            i += 1
            continue
        if not in_sq:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth = max(0, depth - 1)
            elif ch == "," and depth == 0:
                piece = body[start:i].strip()
                if piece:
                    items.append(piece)
                start = i + 1
        i += 1
    tail = body[start:].strip()
    if tail:
        items.append(tail)
    return items


def _parse_columns_and_constraints(body: str) -> tuple[List[Dict[str, object]], List[str]]:
    columns: List[Dict[str, object]] = []
    pks: List[str] = []
    for item in _split_top_level_commas(body):
        clean_item = re.sub(r"\s+", " ", item).strip()
        upper = clean_item.upper()
        # Table-level constraints
        if upper.startswith(("CONSTRAINT ", "PRIMARY KEY", "UNIQUE ", "FOREIGN KEY", "CHECK ")):
            pk_m = re.search(r"\bPRIMARY\s+KEY\s*\(([^)]+)\)", clean_item, re.IGNORECASE)
            if pk_m:
                for col in pk_m.group(1).split(","):
                    col_name = _unquote(col)
                    if col_name and col_name not in pks:
                        pks.append(col_name)
            continue

        # Column definition: "<COL>" TYPE(...) [DEFAULT ...] [NOT NULL]
        col_m = re.match(r'^("?[A-Za-z_][A-Za-z0-9_$#]*"?)\s+(.+)$', clean_item)
        if not col_m:
            continue
        col_name = _unquote(col_m.group(1))
        rest = col_m.group(2)
        # Extract data type (handles `VARCHAR2(140 BYTE)` and `NUMBER(16,2)` cleanly)
        type_m = re.match(
            r"^([A-Za-z0-9_]+(?:\s*\([^)]+\))?(?:\s+(?:WITH\s+TIME\s+ZONE|PRECISION))?)",
            rest,
            re.IGNORECASE,
        )
        data_type = type_m.group(1).strip().upper() if type_m else rest.split()[0].upper()
        nullable = "NOT NULL" not in rest.upper()
        if re.search(r"\bPRIMARY\s+KEY\b", rest.upper()) and col_name not in pks:
            pks.append(col_name)
        columns.append(
            {
                "name": col_name,
                "data_type": data_type,
                "nullable": nullable,
            }
        )
    return columns, pks


def parse_ddl_statements(sql_text: str) -> List[TableSchemaRecord]:
    """Parse CREATE TABLE, ALTER TABLE PRIMARY KEY, and CREATE INDEX statements from DDL text."""
    # Remove SQL comments cleanly
    cleaned = re.sub(r"/\*.*?\*/", " ", sql_text, flags=re.DOTALL)
    cleaned = re.sub(r"--[^\n]*", " ", cleaned)

    tables: Dict[str, TableSchemaRecord] = {}

    for m in _CREATE_TABLE_RE.finditer(cleaned):
        schema = _unquote(m.group("schema") or "")
        tbl = _unquote(m.group("table"))
        if not tbl:
            continue
        body = _extract_balanced_parens(cleaned, m.end() - 1)
        if not body:
            continue
        cols, pks = _parse_columns_and_constraints(body)
        if tbl not in tables:
            tables[tbl] = TableSchemaRecord(
                table_name=tbl,
                schema_name=schema,
                columns=cols,
                primary_keys=pks,
            )
        else:
            if cols:
                tables[tbl].columns = cols
            if pks:
                tables[tbl].primary_keys = list(dict.fromkeys(tables[tbl].primary_keys + pks))
            if schema and not tables[tbl].schema_name:
                tables[tbl].schema_name = schema

    for m in _ALTER_PK_RE.finditer(cleaned):
        tbl = _unquote(m.group("table"))
        cols = [_unquote(c) for c in m.group("cols").split(",") if _unquote(c)]
        if tbl in tables:
            tables[tbl].primary_keys = list(dict.fromkeys(tables[tbl].primary_keys + cols))
        elif cols:
            tables[tbl] = TableSchemaRecord(
                table_name=tbl,
                schema_name=_unquote(m.group("schema") or ""),
                primary_keys=cols,
            )

    for m in _CREATE_INDEX_RE.finditer(cleaned):
        tbl = _unquote(m.group("tbl"))
        idx_name = _unquote(m.group("idx_name"))
        is_unique = bool(m.group("unique"))
        cols = [_unquote(c.split()[0]) for c in m.group("cols").split(",") if _unquote(c)]
        rec = tables.setdefault(
            tbl,
            TableSchemaRecord(table_name=tbl, schema_name=_unquote(m.group("tbl_schema") or "")),
        )
        rec.indexes.append({"name": idx_name, "unique": is_unique, "columns": cols})

    return list(tables.values())
