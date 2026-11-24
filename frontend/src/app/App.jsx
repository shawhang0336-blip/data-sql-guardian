import React from 'react'
import { BrowserRouter, NavLink, Route, Routes } from 'react-router-dom'
import { ProjectProvider, useProjectContext } from '../context/ProjectContext.jsx'
import WorkspacePage from '../features/workspace/WorkspacePage.jsx'
import AnalysisPage from '../features/analysis/AnalysisPage.jsx'
import ReviewPage from '../features/review/ReviewPage.jsx'
import SettingsPage from '../features/platform/SettingsPage.jsx'

function ShellLayout() {
  const { projects, selectedProjectId, setSelectedProjectId, dashboard, refreshProjects } = useProjectContext()

  return (
    <div className="layout-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-title">DS Agent</div>
          <div className="brand-sub">SQL 脚本治理与上线评估台</div>
        </div>
        <nav>
          <NavLink to="/" end className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}>
            <span>1. 项目导入与血缘台</span>
          </NavLink>
          <NavLink to="/analysis" className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}>
            <span>2. 单脚本 & 全局优化</span>
          </NavLink>
          <NavLink to="/review" className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}>
            <span>3. 上线前变更门禁</span>
          </NavLink>
          <NavLink to="/settings" className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}>
            <span>4. 系统与模型配置</span>
          </NavLink>
        </nav>

        <div style={{ marginTop: 'auto', padding: '12px', borderTop: '1px solid #1e293b', fontSize: 12, color: '#94a3b8' }}>
          <div>项目数：{dashboard?.project_count ?? 0} · 脚本：{dashboard?.script_count ?? 0}</div>
          <div>切块：{dashboard?.chunk_count ?? 0} · 待处理告警：{dashboard?.open_finding_count ?? 0}</div>
          <div>待审批变更：{dashboard?.pending_review_count ?? 0}</div>
        </div>
      </aside>

      <div className="main-area">
        <header className="topbar">
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <span style={{ fontWeight: 600, color: '#475569' }}>当前分析项目：</span>
            <select
              className="select"
              style={{ width: 280 }}
              value={selectedProjectId}
              onChange={(e) => setSelectedProjectId(e.target.value)}
            >
              {projects.length === 0 ? (
                <option value="">（尚未导入项目）</option>
              ) : (
                projects.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name} ({p.script_count} 脚本 / {p.chunk_count} 切块)
                  </option>
                ))
              )}
            </select>
            <button type="button" className="btn" onClick={() => refreshProjects()}>
              刷新
            </button>
          </div>
          <div style={{ fontSize: 12, color: '#64748b' }}>
            8-Layer ProdAgent Microkernel · SQLGlot + 16×6 Deterministic Rules
          </div>
        </header>

        <main className="content-wrap">
          <Routes>
            <Route path="/" element={<WorkspacePage />} />
            <Route path="/analysis" element={<AnalysisPage />} />
            <Route path="/review" element={<ReviewPage />} />
            <Route path="/settings" element={<SettingsPage />} />
          </Routes>
        </main>
      </div>
    </div>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <ProjectProvider>
        <ShellLayout />
      </ProjectProvider>
    </BrowserRouter>
  )
}
