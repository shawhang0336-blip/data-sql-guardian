import React from 'react'
import { BrowserRouter, NavLink, Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { ProjectProvider, useProjectContext } from '../context/ProjectContext.jsx'
import { ImportProjectModal } from '../components/ImportProjectModal.jsx'
import { ToastViewport } from '../components/ToastViewport.jsx'
import WorkspacePage from '../features/workspace/WorkspacePage.jsx'
import AnalysisPage from '../features/analysis/AnalysisPage.jsx'
import ReviewPage from '../features/review/ReviewPage.jsx'
import SettingsPage from '../features/platform/SettingsPage.jsx'

function BrandMark() {
  return (
    <svg viewBox="0 0 36 36" width="32" height="32" role="img" aria-hidden="true">
      <defs>
        <linearGradient id="ds-mark-grad" x1="0" y1="0" x2="36" y2="36" gradientUnits="userSpaceOnUse">
          <stop stopColor="#3b82f6" />
          <stop offset="1" stopColor="#1d4ed8" />
        </linearGradient>
      </defs>
      <rect width="36" height="36" rx="9" fill="url(#ds-mark-grad)" />
      <path d="M11 11h14M11 18h10M11 25h6" stroke="#fff" strokeWidth="2.4" strokeLinecap="round" />
      <circle cx="25" cy="24" r="4.5" fill="#10b981" stroke="#1e3a8a" strokeWidth="1.5" />
    </svg>
  )
}

function WorkbenchShell() {
  const {
    projects,
    selectedProjectId,
    setSelectedProjectId,
    backendOnline,
    setImportModalOpen,
  } = useProjectContext()
  const location = useLocation()

  const steps = [
    { path: '/', num: '01', label: '项目导入与血缘' },
    { path: '/analysis', num: '02', label: '单脚本 & 全局优化' },
    { path: '/review', num: '03', label: '上线前变更门禁' },
  ]

  return (
    <div className="app-shell">
      <header className="workbench-header">
        <NavLink to="/" className="workbench-brand">
          <span className="workbench-brand-mark"><BrandMark /></span>
          <span className="workbench-brand-text">
            <span className="workbench-brand-title">DS Agent</span>
            <span className="workbench-brand-sub">SQL 工程治理工作台</span>
          </span>
        </NavLink>

        <nav className="workbench-steps" aria-label="主工作流程">
          {steps.map((s) => (
            <NavLink
              key={s.path}
              to={s.path}
              end={s.path === '/'}
              className={({ isActive }) => `workbench-step ${isActive ? 'active' : ''}`}
            >
              <span className="workbench-step-num">{s.num}</span>
              <span>{s.label}</span>
            </NavLink>
          ))}
        </nav>

        <div className="header-controls">
          <select
            className="header-select"
            aria-label="切换当前分析项目"
            value={selectedProjectId}
            onChange={(e) => setSelectedProjectId(e.target.value)}
          >
            {projects.length === 0 ? (
              <option value="">（尚未导入项目）</option>
            ) : (
              projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name} ({p.script_count} 脚本)
                </option>
              ))
            )}
          </select>

          <button
            type="button"
            className="header-btn header-btn-primary"
            onClick={() => setImportModalOpen(true)}
          >
            + 导入项目
          </button>

          <NavLink
            to="/settings"
            className={`header-btn ${location.pathname === '/settings' ? 'header-btn-primary' : ''}`}
          >
            系统设置
          </NavLink>

          <span className="health-pill" title={backendOnline ? '后端 API 运行正常' : '无法连接后端 API'}>
            <span className={`health-dot ${backendOnline ? '' : 'offline'}`} />
            {backendOnline ? '在线' : '离线'}
          </span>
        </div>
      </header>

      <main className="main-stage">
        <Routes>
          <Route path="/" element={<WorkspacePage />} />
          <Route path="/analysis" element={<AnalysisPage />} />
          <Route path="/review" element={<ReviewPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>

      <ImportProjectModal />
      <ToastViewport />
    </div>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <ProjectProvider>
        <WorkbenchShell />
      </ProjectProvider>
    </BrowserRouter>
  )
}
