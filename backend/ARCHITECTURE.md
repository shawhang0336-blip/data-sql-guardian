# DS Agent 后端架构说明（8 层单向有向无环图 + ProdAgent 微内核）

严格参考 `dq_agent` 工程治理底座构建，全后端遵循**严格单向分层 DAG** 与 **「机制在内，策略在外」** 原则。

## 1. 严格 8 层单向依赖 DAG

```text
Layer 0: app.infra       （配置/日志/数据库12表/BlobStore/TaskRunner/受控LLM封装与RateLimiter）
   ▲
Layer 1: app.kernel      （ProdAgent 六模块纯微内核：plan, bus, event_log, interrupt, run, scheduler）
   ▲
Layer 2: app.policy      （领域蓝图 ds_plan、5大确定性事前门禁 adjudicators ds_bus、通道 ds_channels）
   ▲
Layer 3: app.ingestion   （纯离线 parsers/：dialect_chunker, ast_analyzer, ddl_parser, archive_loader）
   ▲
Layer 4: app.catalog     （工程资产入仓 project_service、跨脚本血缘 DAG lineage_graph、AST Diff）
   ▲
Layer 5: app.analysis    （纯离线 core/ 16+6规则 + workflow/ 单脚本/全仓 AI 协同编排）
   ▲
Layer 6: app.review      （变更评估 change_evaluator、Blast Radius、Gate 红线、Interrupt 审批合入）
   ▲
Layer 7: app.api         （四域路由：platform, workspace, analysis, review，严禁路由跨导）
   ▲
Layer 8: app.main        （FastAPI 根装配与 lifespan 断点恢复）
```

## 2. 隔离与纯度红线（由 `tests/test_kernel_policy_isolation.py` AST 守护）

- **`app/` 根目录** 仅允许存在 `__init__.py`、`main.py` 与上述 8 个层级包。
- **`app/kernel/`** 仅含 6 个内核模块，零任何业务层或策略层引用。
- **`app/policy/`** 仅含 `ds_plan.py`、`ds_bus.py`、`ds_channels.py`，import 期零全局状态修改。
- **`app/ingestion/parsers/` 与 `app/analysis/core/`** 为纯离线内核，禁止出现 SQLAlchemy ORM、DB 会话或 LLM Client 依赖。
- **`app/api/{domain}/`** 严格按四域划分，禁止路由互导。

## 3. 血缘口径与 5 大 `ds_bus` 确定性事前门禁

1. **DDL 与 ETL 血缘分层**：`lineage_graph.py` 构建运行时 `DATA_FLOW`、`WRITE_CONFLICT` 与 `TRUNCATE_CONFLICT` 边时，明确跳过 `file_kind == "DDL"` 的建表文件，避免将表定义误判为运行期并发写入。
2. **5 道 `ds_bus` 确定性事前门禁（`app/policy/ds_bus.py`）**：
   - `chunk_id_whitelist_adjudicator`：拦截幻觉 `chunk_id` 与越界 `priority_chunk_ids`。
   - `entity_whitelist_adjudicator`：拦截全项目建议中捏造的虚构表名。
   - `severity_category_adjudicator`：强制严重级别与缺陷分类枚举合法。
   - `sql_rewrite_syntax_adjudicator`：借助 `sqlglot` 多方言校验 `rewritten_sql` 合法性，禁止擅自 `DROP TABLE` 或丢失目标写入表。
   - `release_gate_floor_adjudicator`：确定性门禁判为 `BLOCK` 时，严禁大模型将其降级为 `WARN` 或 `PASS`。

## 4. 数据库 12 张核心表（`app/infra/db_tables.py`）

1. `runs` / `events`：ProdAgent 事件溯源账本与 SHA-256 防篡改哈希链。
2. `system_settings`：运行期 LLM 配置与两级并发/成本预算护栏。
3. `script_projects` / `script_files` / `script_chunks` / `table_schemas` / `lineage_edges`：全仓资产、AST 特征指纹与血缘拓扑。
4. `optimization_findings`：单脚本与全局级优化发现、`rewritten_sql` 与采纳/忽略状态机。
5. `change_reviews` / `script_revisions` / `review_approvals`：上线前评估单、版本快照与审批合入留痕。
