# Data SQL Guardian（`data-sql-guardian`）

> **统一 Data Agent 产品家族**：[`data-rule-studio`](https://github.com/shawhang0336-blip/data-rule-studio)（规则装配与验证） · [`data-biz-copilot`](https://github.com/shawhang0336-blip/data-biz-copilot)（业务资产与制度对账） · [`data-sql-guardian`](https://github.com/shawhang0336-blip/data-sql-guardian)（SQL 优化与发布门禁）

面向金融与数仓 ETL SQL / Oracle PL-SQL 项目的**整仓切片解析、单脚本诊断重写、全局架构优化与上线前变更门禁**智能工程治理台。

## 核心能力

1. **整仓导入与精确切块解析**
   - 支持本地目录递归导入、ZIP 安全解压（Zip Slip / 体积上限防护）以及一键导入 25 个真实 **SAC Oracle ETL 脚本（16,274 行）**。
   - 方言感知切块引擎（`dialect_chunker.py`）：按语句级精确拆块并保留 `start_line` / `end_line`、控制流上下文（严格区分 SQL `CASE ... END` 与 PL/SQL `IF ... ELSE ... END IF`）以及解包 `EXECUTE IMMEDIATE '...'` 动态 SQL。
   - DDL 与跨脚本血缘（`ddl_parser.py` & `lineage_graph.py`）：兼容 Oracle `"SCHEMA"."TABLE"` + `VARCHAR2(N BYTE)` + `TABLESPACE`，自动剔除纯 DDL 定义文件干扰，精准构建 `DATA_FLOW`、`WRITE_CONFLICT`、`TRUNCATE_CONFLICT` 有向图，识别环路（DAG Cycles）、核心枢纽表、长串行链路与跨脚本重复逻辑簇。

2. **单脚本优化（16 条确定性规则 + AI 根因诊断 & SQL 重写）**
   - `S001`–`S016` 纯离线静态规则覆盖：`NULL` 等值子查询死锁（`S001`）、无 `WHERE` 改删（`S002`）、`NOT IN` 空值陷阱（`S003`）、缺 `EXCEPTION` 回滚块（`S004`）、`TRUNCATE` 混用多点 `COMMIT`（`S005`）、`JOIN` / `WHERE` 列包裹函数（`S006`/`S007`）、前导通配符 `LIKE`（`S008`）、无列名 `INSERT`（`S009`）、`SYS_GUID()` 非确定主键（`S010`）、隐式笛卡尔积（`S011`）、动态 SQL 风险（`S012`）、单脚本反复写入同表（`S013`）、`SELECT *`（`S014`）、运行时 `SYSDATE` 分支（`S015`）及 `DISTINCT` / 非 `ALL` `UNION`（`S016`）。
   - 受控 AI 介入：先运行 `plan_script_review` 精选高优切块，再以 BSP 并行波次调用 `diagnose_chunk_issues` 与 `propose_sql_rewrite`，经 `ds_bus` + `sqlglot` 语法与目标表门禁校验后入库。

3. **全项目全局优化（6 条确定性规则 + AI 架构重构路线图）**
   - `P001` 跨脚本并发写 / `TRUNCATE` 冲突、`P002` 血缘环路依赖、`P003` 跨脚本重复逻辑簇、`P004` 过长串行关键路径、`P005` 高扇入/出枢纽表瓶颈、`P006` 孤儿中间表与缺失 DDL 资产。
   - 受 `entity_whitelist_adjudicator` 约束，禁止 AI 幻觉捏造表名。

4. **上线前变更评估与发布门禁（Pre-Release Gate）**
   - 支持提交 `ADD` 新增脚本或 `MODIFY` 基线脚本修改，计算 Unified Diff + AST 增量差异 + 临时模拟拓扑中的多跳下游 **Blast Radius（爆破半径）**。
   - 确定性裁决红线：`PASS` / `WARN` / `BLOCK`（新增 P0、移除 `EXCEPTION`、新 `TRUNCATE`/写冲突或血缘环路强制 `BLOCK`），并挂载 `release_gate_floor_adjudicator` 禁止 AI 降级硬红线。
   - 通过 ProdAgent `Interrupt` 协议挂起等待人工审批；批准上线后自动合并入项目基线、`ScriptRevisionModel` 版本递增并重建全仓血缘；支持导出 Markdown 报告与查看防篡改 SHA-256 事件溯源链。

## 四域 REST API 概览（`/api`）

| 域 | 核心接口 | 说明 |
| --- | --- | --- |
| **Workspace** | `POST /api/projects/import-sac`、`import-dir`、`import-zip`、`GET /api/projects/{id}/lineage` | 整仓导入、脚本/切块详情、DDL 字典、血缘拓扑与重算 |
| **Analysis** | `POST /api/projects/{id}/analyze`、`scripts/{sid}/analyze`、`GET /api/projects/{id}/findings`、`PATCH /api/findings/{fid}` | 全项目/单脚本分析、优化清单过滤与 `ADOPTED`/`IGNORED` 状态流转 |
| **Review** | `POST /api/projects/{id}/reviews`、`POST /api/reviews/{rid}/approve`、`reject`、`GET /api/reviews/{rid}/export`、`GET /api/runs/{id}/trace` | 变更评估、门禁审批合入基线、Markdown 导出与哈希链校验 |
| **Platform** | `GET /api/system/health`、`dashboard`、`GET/PUT /api/settings`、`POST /api/settings/llm/test` | 健康检查、全局指标、脱敏 LLM 与 BudgetGuard 配置热更新 |

## 实测验证基线（真实 SAC 语料）

- 导入 `25` 个 SAC Oracle SQL 脚本（16,274 行），精准切分 `1,651` 个语句切块与 `183` 张 DDL 数据表。
- 剔除 DDL 定义噪声后提取 `173` 条跨脚本 ETL 依赖边（含 `8` 组真写冲突与 `32` 组 `TRUNCATE` 冲突），精准命中 `SP_SAC_ACCT_AMOUNT.sql` 的 `NULL` 谓词逻辑死锁（`S001`）、`REPLACE` 连接（`S006`）与重复写入同表（`S013`），以及 `SP_SAC_LARGE_BULU.sql` 的函数包裹 JOIN（`S006`）、`LIKE '%购汇%'`（`S008`）、`SYS_GUID()`（`S010`）与 `SYSDATE` 时段分支（`S015`）。
- 全量测试通过：`17` 项后端 pytest（`-W error::sqlalchemy.exc.SAWarning` 零警告）+ `4` 项前端审计测试及生产构建。

## 环境配置与快速启动

```bash
# 1. 复制配置模板（按需填入真实 LLM_API_KEY；默认 scripted 离线模式即可完整运行确定性能力）
cp .env.example .env

# 2. 启动后端（FastAPI，端口 8020）
cd backend
pip install -e .
uvicorn app.main:app --host 127.0.0.1 --port 8020

# 3. 启动前端（Vite + React 19，端口 5175，自动代理 /api -> 127.0.0.1:8020）
cd frontend
npm install
npm run dev
```

## 测试命令

```bash
# 后端全量测试（8 层 DAG/纯度检查 + SAC 真实语料 + 变更门禁 + 四域 API E2E）
cd backend && pytest -v -W error::sqlalchemy.exc.SAWarning

# 前端目录/API 隔离审计与生产构建
cd frontend && npm test && npm run build
```
