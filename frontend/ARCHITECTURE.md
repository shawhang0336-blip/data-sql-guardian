# DS Agent 前端架构说明

对齐后端四域 API（`platform` / `workspace` / `analysis` / `review`），采用与 `dq_agent` 一致的**横向工作台导航壳 + GitHub-Light 阅读级代码块 + 主从左右分栏（Master-Detail）+ 分页筛选器**设计，严格保持 8 目录结构与零 `/api/` 泄漏约束（由 `tests/frontend_audit.test.mjs` 守护）。

## 1. 目录职责（`src/` 仅允许以下 8 个目录）

```text
src/
├── api/          # 唯一允许调用 fetch() 与 '/api/' 的通信层（client, platform, workspace, analysis, review）
├── app/          # 顶部横向步骤工作台壳（App.jsx：项目切换、+导入弹窗入口、系统设置、在线健康状态点）
├── components/   # 通用组件（PageHeader, StatusBadge, CodeBlock, CodeDiffView, EmptyState, ImportProjectModal, ToastViewport）
├── context/      # 全局项目状态、后端连通态、导入弹窗与浮动 Toast 队列（ProjectContext.jsx）
├── features/     # 与后端 API 严格对称的 4 大业务域页面（workspace, analysis, review, platform）
├── hooks/        # 异步操作与错误态通用钩子（useAsync.js）
├── styles/       # 柔和暖灰底色 + GitHub-Light 代码高亮 + 主从分栏响应式样式系统（index.css）
└── utils/        # 纯函数分页、问题聚合与时间状态映射标签（labels.js）
```

## 2. 核心页面交互流转

1. **01 项目导入与血缘 (`features/workspace/`)**：
   - 空库时居中引导「一键导入 SAC 样本库」或自定义目录 / ZIP 导入（常驻入口收纳至顶栏 `+ 导入项目` 弹窗，减少主页面视觉噪点）。
   - 主界面通过 3 个分段 Tab 隔离信息维度：**脚本与切块剖面**（Master-Detail 左右分栏 + 按语句切块 / 完整源码切换 + 带行号与一键复制的亮色代码块）、**跨脚本血缘与冲突**（分页拓扑边表与写/清表冲突筛选）、**DDL 表结构字典**（可展开字段/类型/主键）。
2. **02 单脚本 & 全局优化 (`features/analysis/`)**：
   - 顶部 4 张可点击 KPI 卡片直接联动过滤 `P0`/`P1`/`P2` 问题。
   - **问题排查清单**采用 `20 条/页` 分页 + 默认收起的手风琴展开抽屉（展开显示 AI 根因诊断提示框、修复建议、亮色证据片段及校验通过的重写 SQL）；支持**按脚本分布汇总**视图及 **AI 全项目重构路线图**视图。
3. **03 上线前变更门禁 (`features/review/`)**：
   - Master-Detail 变更列表按 `BLOCK` / `WARN` / `PASS` 色条左对齐标注；右侧展示状态总览 Hero 横幅、直接审批合并按钮，并通过 4 个清晰 Tab 拆解**爆破半径**、**GitHub-Light Diff**、**增量告警与 AI 审查**、**Markdown 报告与 SHA-256 事件哈希链**。
4. **系统设置 (`features/platform/`)**：
   - 两栏聚焦配置大模型 Provider（脱敏保护 + 一键测试反馈）与 BudgetGuard 限流熔断阈值。
