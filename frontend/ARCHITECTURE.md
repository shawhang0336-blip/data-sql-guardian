# DS Agent 前端架构说明

对齐后端四域 API（`platform` / `workspace` / `analysis` / `review`），前端保持严格 8 目录物理结构与零 `/api/` 泄漏约束（由 `tests/frontend_audit.test.mjs` 守护）。

## 1. 目录职责（`src/` 仅允许以下 8 个目录）

```text
src/
├── api/          # 唯一允许调用 fetch() 与 '/api/' 的通信层（client, platform, workspace, analysis, review）
├── app/          # Router 根壳与侧边栏项目切换入口（App.jsx）
├── components/   # 无状态通用组件（PageHeader, StatusBadge, CodeDiffView）
├── context/      # 全局项目选中与仪表盘上下文（ProjectContext.jsx）
├── features/     # 与后端 API 严格对称的 4 大业务域页面（workspace, analysis, review, platform）
├── hooks/        # 异步操作与错误态通用钩子（useAsync.js）
├── styles/       # 全局设计样式系统（index.css）
└── utils/        # 纯函数格式化与状态映射标签（labels.js）
```

## 2. 核心页面流转

1. **1. 项目导入与血缘台 (`features/workspace/`)**：一键导入 25 个真实 SAC 脚本 / 本地目录 / ZIP，按切块高亮浏览源码、`start_line`/`end_line` 与 `DATA_FLOW` / `WRITE_CONFLICT` 血缘边。
2. **2. 单脚本 & 全局优化 (`features/analysis/`)**：触发全仓 16×6 规则体检与受控 AI 诊断，支持 `PROJECT`/`SCRIPT` 与 `P0`/`P1`/`P2` 过滤、查看等价 SQL 重写以及采纳/忽略闭环。
3. **3. 上线前变更门禁 (`features/review/`)**：在线提交 `ADD` / `MODIFY` 变更，查看 Unified Diff、多跳爆破半径表、`BLOCK`/`WARN`/`PASS` 裁决及 SHA-256 哈希链验证，执行人工批准合并基线或驳回并导出 Markdown。
4. **4. 系统与模型配置 (`features/platform/`)**：热更新 LLM Provider（密钥脱敏）、连通性自检及 BudgetGuard 参数。
