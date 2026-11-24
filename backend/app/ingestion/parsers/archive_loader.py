"""Pure-offline archive and directory script loaders (Zip Slip protected)."""

from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

ALLOWED_SCRIPT_EXTS = {".sql", ".pls", ".pkb", ".pks", ".prc", ".fnc", ".ddl", ".hql"}
SKIP_DIR_NAMES = {"__MACOSX", ".git", ".venv", "node_modules", "__pycache__"}
MAX_ZIP_FILES = 5000
MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB per SQL script
MAX_TOTAL_BYTES = 100 * 1024 * 1024  # 100 MB total project archive


@dataclass
class CollectedScript:
    """In-memory script file loaded from disk or zip archive."""

    filename: str
    relative_path: str
    content: str
    sha256: str


def normalize_table_identifier(raw: str) -> str:
    """Strip quotes and schema prefix to normalize table name."""
    parts = [p.strip().strip('"').strip("`").upper() for p in raw.split(".") if p.strip()]
    return parts[-1] if parts else ""


def _decode_bytes(raw: bytes) -> str:
    for enc in ("utf-8", "utf-8-sig", "gb18030", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def extract_zip_safely(zip_bytes: bytes, target_dir: Path) -> List[CollectedScript]:
    """Extract SQL scripts from a ZIP archive into `target_dir` with Zip Slip and bomb guards."""
    target_root = target_dir.resolve()
    target_root.mkdir(parents=True, exist_ok=True)
    collected: List[CollectedScript] = []
    total_bytes = 0

    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        members = [m for m in zf.infolist() if not m.is_dir()]
        if len(members) > MAX_ZIP_FILES:
            raise ValueError(f"压缩包文件数超过上限 ({len(members)} > {MAX_ZIP_FILES})")

        for info in members:
            rel_parts = Path(info.filename).parts
            if any(part in SKIP_DIR_NAMES or part.startswith("._") for part in rel_parts):
                continue
            ext = Path(info.filename).suffix.lower()
            if ext not in ALLOWED_SCRIPT_EXTS:
                continue
            if info.file_size > MAX_FILE_BYTES:
                raise ValueError(f"单文件体积超过上限 ({info.filename}: {info.file_size} bytes)")
            total_bytes += info.file_size
            if total_bytes > MAX_TOTAL_BYTES:
                raise ValueError("解压总大小超过 100MB 安全上限")

            dest = (target_root / info.filename).resolve()
            if not dest.is_relative_to(target_root):
                raise ValueError(f"检测到非法 ZIP 路径穿越: {info.filename}")

            raw = zf.read(info.filename)
            if len(raw) > MAX_FILE_BYTES:
                raise ValueError(f"解压后单文件体积超限: {info.filename}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(raw)

            text = _decode_bytes(raw)
            sha = hashlib.sha256(raw).hexdigest()
            collected.append(
                CollectedScript(
                    filename=dest.name,
                    relative_path=Path(info.filename).as_posix(),
                    content=text,
                    sha256=sha,
                )
            )

    collected.sort(key=lambda c: (0 if "SCHEMA" in c.filename.upper() else 1, c.relative_path))
    return collected


def collect_directory_scripts(dir_path: str | Path) -> List[CollectedScript]:
    """Recursively read SQL/DDL files from a local project directory."""
    root = Path(dir_path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"项目目录不存在: {root}")

    collected: List[CollectedScript] = []
    total_bytes = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        if path.suffix.lower() not in ALLOWED_SCRIPT_EXTS:
            continue
        raw = path.read_bytes()
        if len(raw) > MAX_FILE_BYTES:
            continue
        total_bytes += len(raw)
        if total_bytes > MAX_TOTAL_BYTES:
            break
        text = _decode_bytes(raw)
        collected.append(
            CollectedScript(
                filename=path.name,
                relative_path=path.relative_to(root).as_posix(),
                content=text,
                sha256=hashlib.sha256(raw).hexdigest(),
            )
        )

    collected.sort(key=lambda c: (0 if "SCHEMA" in c.filename.upper() else 1, c.relative_path))
    return collected


def collect_real_sac_scripts(sql_explorer_dir: str | Path) -> Tuple[Path, List[CollectedScript]]:
    """Load the 25 real SAC Oracle/ETL SQL scripts from `sql_explorer/sample_scripts`."""
    root = Path(sql_explorer_dir).expanduser().resolve()
    samples_dir = root / "sample_scripts"
    if not samples_dir.exists() or not samples_dir.is_dir():
        raise FileNotFoundError(f"未找到 sql_explorer 示例目录: {samples_dir}")

    files = sorted(samples_dir.glob("*SAC*.sql"))
    if not files:
        raise FileNotFoundError(f"目录 {samples_dir} 中未匹配到 *SAC*.sql 脚本")

    collected: List[CollectedScript] = []
    for path in files:
        raw = path.read_bytes()
        collected.append(
            CollectedScript(
                filename=path.name,
                relative_path=f"sample_scripts/{path.name}",
                content=_decode_bytes(raw),
                sha256=hashlib.sha256(raw).hexdigest(),
            )
        )
    collected.sort(key=lambda c: (0 if "SCHEMA" in c.filename.upper() else 1, c.filename))
    return samples_dir, collected
