"""统一 ORM 表模型定义（基础设施层 Layer 0，12 张核心表）。

分为三组：
1. 内核运行时账本与系统设置：`RunModel` / `EventModel` / `SystemSettingModel`；
2. 工程项目、脚本切块、DDL 字典与跨脚本血缘：`ScriptProjectModel` / `ScriptFileModel` /
   `ScriptChunkModel` / `TableSchemaModel` / `LineageEdgeModel`；
3. 单脚本与全项目优化建议、上线前变更评估与审批：`OptimizationFindingModel` /
   `ChangeReviewModel` / `ScriptRevisionModel` / `ReviewApprovalModel`。
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.db import Base

_LONG_TEXT = Text().with_variant(LONGTEXT(), "mysql")


# =============================================================================
# 1. 内核运行时账本与系统设置
# =============================================================================


class RunModel(Base):
    """Run 身份表，状态完全由 EventModel 事件账本折叠决定。"""

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    spec_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class EventModel(Base):
    __tablename__ = "events"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    kind: Mapped[str] = mapped_column(String(64))
    node: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[Dict[str, Any]] = mapped_column(JSON)
    prev_hash: Mapped[str] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SystemSettingModel(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(96), primary_key=True)
    value_json: Mapped[Dict[str, Any]] = mapped_column(JSON)
    secret_reference: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# =============================================================================
# 2. 工程项目、脚本切块、DDL 表结构字典与血缘图谱
# =============================================================================


class ScriptProjectModel(Base):
    __tablename__ = "script_projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    # SAC_BUILTIN | ZIP_ARCHIVE | LOCAL_DIR | MANUAL
    source_mode: Mapped[str] = mapped_column(String(32), default="MANUAL")
    source_path: Mapped[str] = mapped_column(String(512), default="")
    dialect: Mapped[str] = mapped_column(String(32), default="oracle")
    status: Mapped[str] = mapped_column(String(32), index=True, default="READY")
    script_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    table_count: Mapped[int] = mapped_column(Integer, default=0)
    finding_count: Mapped[int] = mapped_column(Integer, default=0)
    summary_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ScriptFileModel(Base):
    __tablename__ = "script_files"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("script_projects.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255), index=True)
    relative_path: Mapped[str] = mapped_column(String(512), default="")
    dialect: Mapped[str] = mapped_column(String(32), default="oracle")
    # ETL_SCRIPT | DDL_SCHEMA | FUNCTION | PACKAGE
    file_kind: Mapped[str] = mapped_column(String(32), index=True, default="ETL_SCRIPT")
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    blob_uri: Mapped[str] = mapped_column(String(255))
    line_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    target_tables_json: Mapped[List[str]] = mapped_column(JSON, default=list)
    source_tables_json: Mapped[List[str]] = mapped_column(JSON, default=list)
    ast_fingerprint: Mapped[str] = mapped_column(String(64), index=True, default="")
    has_exception_handler: Mapped[bool] = mapped_column(Boolean, default=False)
    commit_count: Mapped[int] = mapped_column(Integer, default=0)
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32), index=True, default="PARSED")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ScriptChunkModel(Base):
    __tablename__ = "script_chunks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("script_projects.id"), index=True)
    script_id: Mapped[str] = mapped_column(ForeignKey("script_files.id"), index=True)
    chunk_index: Mapped[int] = mapped_column(Integer)
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    statement_kind: Mapped[str] = mapped_column(String(32), index=True)
    is_dynamic_sql: Mapped[bool] = mapped_column(Boolean, default=False)
    control_context: Mapped[str] = mapped_column(String(64), default="TOP_LEVEL")
    target_tables_json: Mapped[List[str]] = mapped_column(JSON, default=list)
    source_tables_json: Mapped[List[str]] = mapped_column(JSON, default=list)
    ast_features_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    ast_fingerprint: Mapped[str] = mapped_column(String(64), index=True, default="")
    raw_sql: Mapped[str] = mapped_column(_LONG_TEXT)
    comments_json: Mapped[List[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TableSchemaModel(Base):
    __tablename__ = "table_schemas"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("script_projects.id"), index=True)
    table_name: Mapped[str] = mapped_column(String(255), index=True)
    schema_name: Mapped[str] = mapped_column(String(128), default="")
    columns_json: Mapped[List[Dict[str, Any]]] = mapped_column(JSON, default=list)
    primary_keys_json: Mapped[List[str]] = mapped_column(JSON, default=list)
    indexes_json: Mapped[List[Dict[str, Any]]] = mapped_column(JSON, default=list)
    source_script_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class LineageEdgeModel(Base):
    __tablename__ = "lineage_edges"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("script_projects.id"), index=True)
    upstream_script_id: Mapped[str] = mapped_column(ForeignKey("script_files.id"), index=True)
    downstream_script_id: Mapped[str] = mapped_column(ForeignKey("script_files.id"), index=True)
    via_table: Mapped[str] = mapped_column(String(255), index=True)
    # DATA_FLOW | WRITE_CONFLICT | TRUNCATE_CONFLICT
    edge_kind: Mapped[str] = mapped_column(String(32), index=True, default="DATA_FLOW")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# =============================================================================
# 3. 单脚本 & 全项目优化建议、上线前变更评估与人工审批台账
# =============================================================================


class OptimizationFindingModel(Base):
    __tablename__ = "optimization_findings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("script_projects.id"), index=True)
    # SCRIPT | PROJECT
    scope: Mapped[str] = mapped_column(String(16), index=True, default="SCRIPT")
    script_id: Mapped[Optional[str]] = mapped_column(ForeignKey("script_files.id"), index=True, nullable=True)
    chunk_id: Mapped[Optional[str]] = mapped_column(ForeignKey("script_chunks.id"), index=True, nullable=True)
    rule_code: Mapped[str] = mapped_column(String(64), index=True)
    # CORRECTNESS | PERFORMANCE | IDEMPOTENCY | TRANSACTION | MAINTAINABILITY | ARCHITECTURE
    category: Mapped[str] = mapped_column(String(32), index=True)
    # P0_BLOCKER | P1_HIGH | P2_MEDIUM | P3_LOW
    severity: Mapped[str] = mapped_column(String(24), index=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(_LONG_TEXT, default="")
    evidence_snippet: Mapped[str] = mapped_column(_LONG_TEXT, default="")
    start_line: Mapped[int] = mapped_column(Integer, default=0)
    end_line: Mapped[int] = mapped_column(Integer, default=0)
    related_tables_json: Mapped[List[str]] = mapped_column(JSON, default=list)
    related_scripts_json: Mapped[List[str]] = mapped_column(JSON, default=list)
    suggestion: Mapped[str] = mapped_column(_LONG_TEXT, default="")
    rewritten_sql: Mapped[str] = mapped_column(_LONG_TEXT, default="")
    expected_benefit: Mapped[str] = mapped_column(Text, default="")
    ai_refined: Mapped[bool] = mapped_column(Boolean, default=False)
    # OPEN | ACCEPTED | IGNORED
    status: Mapped[str] = mapped_column(String(24), index=True, default="OPEN")
    resolution_note: Mapped[str] = mapped_column(Text, default="")
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ChangeReviewModel(Base):
    __tablename__ = "change_reviews"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("script_projects.id"), index=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    # ADD | MODIFY
    change_type: Mapped[str] = mapped_column(String(16), index=True)
    target_script_id: Mapped[Optional[str]] = mapped_column(ForeignKey("script_files.id"), index=True, nullable=True)
    filename: Mapped[str] = mapped_column(String(255))
    relative_path: Mapped[str] = mapped_column(String(512), default="")
    author: Mapped[str] = mapped_column(String(80), default="研发工程师")
    change_summary: Mapped[str] = mapped_column(Text, default="")
    old_blob_uri: Mapped[str] = mapped_column(String(255), default="")
    new_blob_uri: Mapped[str] = mapped_column(String(255), default="")
    diff_summary_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    blast_radius_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    new_findings_json: Mapped[List[Dict[str, Any]]] = mapped_column(JSON, default=list)
    # PASS | WARN | BLOCK
    deterministic_verdict: Mapped[str] = mapped_column(String(16), index=True, default="PASS")
    final_verdict: Mapped[str] = mapped_column(String(16), index=True, default="PASS")
    ai_review_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    # RUNNING | WAITING_APPROVAL | APPROVED | REJECTED | FAILED
    status: Mapped[str] = mapped_column(String(24), index=True, default="WAITING_APPROVAL")
    merged_to_baseline: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ScriptRevisionModel(Base):
    __tablename__ = "script_revisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("script_projects.id"), index=True)
    script_id: Mapped[str] = mapped_column(ForeignKey("script_files.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    blob_uri: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64))
    author: Mapped[str] = mapped_column(String(80), default="系统基线")
    change_summary: Mapped[str] = mapped_column(Text, default="")
    review_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ReviewApprovalModel(Base):
    __tablename__ = "review_approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    review_id: Mapped[str] = mapped_column(ForeignKey("change_reviews.id"), index=True)
    # APPROVE | REJECT
    decision: Mapped[str] = mapped_column(String(16))
    reviewer: Mapped[str] = mapped_column(String(80))
    comment: Mapped[str] = mapped_column(Text, default="")
    merge_baseline: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
