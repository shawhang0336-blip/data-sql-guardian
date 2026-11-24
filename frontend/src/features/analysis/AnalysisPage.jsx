import React, { useEffect, useMemo, useState } from 'react'
import { PageHeader } from '../../components/PageHeader.jsx'
import { StatusBadge } from '../../components/StatusBadge.jsx'
import { CodeBlock } from '../../components/CodeBlock.jsx'
import { EmptyState } from '../../components/EmptyState.jsx'
import { useProjectContext } from '../../context/ProjectContext.jsx'
import { useAsync } from '../../hooks/useAsync.js'
import { groupFindingsBySeverity, paginateItems } from '../../utils/labels.js'
import { getProjectDetail } from '../../api/workspace.js'
import {
  listFindings,
  runProjectAnalysis,
  runScriptAnalysis,
  updateFindingStatus,
} from '../../api/analysis.js'

export default function AnalysisPage() {
  const { selectedProjectId, refreshProjects, setImportModalOpen, pushToast } = useProjectContext()
  const { busy, error, setError, run } = useAsync()

  const [tab, setTab] = useState('findings') // findings | scripts | roadmap
  const [scripts, setScripts] = useState([])
  const [allFindings, setAllFindings] = useState([])
  const [roadmap, setRoadmap] = useState(null)

  const [severityFilter, setSeverityFilter] = useState('')
  const [scopeFilter, setScopeFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [scriptFilter, setScriptFilter] = useState('')
  const [searchText, setSearchText] = useState('')
  const [page, setPage] = useState(1)
  const [expandedId, setExpandedId] = useState('')

  const loadData = async (pid) => {
    if (!pid) {
      setScripts([])
      setAllFindings([])
      setRoadmap(null)
      return
    }
    const [proj, fRes] = await Promise.all([getProjectDetail(pid), listFindings(pid)])
    setScripts(proj?.scripts || [])
    setRoadmap(proj?.summary?.ai_roadmap || null)
    setAllFindings(fRes?.findings || [])
  }

  useEffect(() => {
    loadData(selectedProjectId).catch((e) => setError(e?.message || '加载分析结果失败'))
  }, [selectedProjectId])

  const counts = useMemo(() => groupFindingsBySeverity(allFindings), [allFindings])

  const filteredFindings = useMemo(() => {
    const q = searchText.trim().toLowerCase()
    return allFindings.filter((f) => {
      if (severityFilter && f.severity !== severityFilter) return false
      if (scopeFilter && f.scope !== scopeFilter) return false
      if (statusFilter && f.status !== statusFilter) return false
      if (scriptFilter && f.script_id !== scriptFilter) return false
      if (!q) return true
      return (
        f.title.toLowerCase().includes(q) ||
        f.rule_code.toLowerCase().includes(q) ||
        (f.script_filename || '').toLowerCase().includes(q) ||
        (f.description || '').toLowerCase().includes(q)
      )
    })
  }, [allFindings, severityFilter, scopeFilter, statusFilter, scriptFilter, searchText])

  const paged = useMemo(() => paginateItems(filteredFindings, page, 20), [filteredFindings, page])

  const scriptSummaryRows = useMemo(() => {
    const map = {}
    for (const s of scripts) {
      map[s.id] = { id: s.id, filename: s.filename, file_kind: s.file_kind, P0: 0, P1: 0, P2: 0, total: 0 }
    }
    for (const f of allFindings) {
      if (!f.script_id || !map[f.script_id]) continue
      map[f.script_id].total += 1
      if (f.severity === 'P0') map[f.script_id].P0 += 1
      else if (f.severity === 'P1') map[f.script_id].P1 += 1
      else map[f.script_id].P2 += 1
    }
    return Object.values(map).sort((a, b) => b.P0 - a.P0 || b.total - a.total)
  }, [scripts, allFindings])

  const handleAnalyzeProject = (withLlm) =>
    run(async () => {
      const res = await runProjectAnalysis(selectedProjectId, { run_llm: withLlm })
      pushToast(
        res.ai_status === 'BILLING_OR_AUTH_ERROR' ? 'error' : 'success',
        `全仓扫描完成：单脚本规则 ${res.script_findings} 条，全局规则 ${res.project_findings} 条（AI：${res.ai_status}）`,
      )
      await loadData(selectedProjectId)
      await refreshProjects(selectedProjectId)
    })

  const handleAnalyzeSingle = (sid, fname, withLlm) =>
    run(async () => {
      const res = await runScriptAnalysis(selectedProjectId, sid, { run_llm: withLlm })
      pushToast(
        res.ai_status === 'BILLING_OR_AUTH_ERROR' ? 'error' : 'success',
        `${fname} 剖析完成：共 ${res.finding_count} 条问题（AI：${res.ai_status}）`,
      )
      await loadData(selectedProjectId)
      await refreshProjects(selectedProjectId)
    })

  const handleMutateStatus = (e, fid, status) => {
    e.stopPropagation()
    run(async () => {
      await updateFindingStatus(fid, status)
      await loadData(selectedProjectId)
    })
  }

  if (!selectedProjectId) {
    return (
      <EmptyState
        title="请先选择或导入 SQL 项目"
        description="导入项目后，即可一键运行 16 条单脚本静默缺陷检测、6 条全仓血缘架构规则以及受控 AI SQL 重写。"
        primaryLabel="+ 导入项目"
        onPrimary={() => setImportModalOpen(true)}
      />
    )
  }

  return (
    <div>
      <PageHeader
        title="单脚本深度剖析 & 全项目全局优化"
        subtitle="分级聚焦排查 P0 阻断逻辑漏洞、P1 性能与写法隐患、以及跨脚本全局表冲突和架构重组建议"
        actions={
          <>
            <button type="button" className="btn" disabled={busy} onClick={() => handleAnalyzeProject(true)}>
              AI 全仓架构合成
            </button>
            <button type="button" className="btn btn-primary" disabled={busy} onClick={() => handleAnalyzeProject(false)}>
              {busy ? '扫描执行中...' : '运行离线全仓规则体检'}
            </button>
          </>
        }
      />

      {error && <div className="alert alert-error">{error}</div>}

      <div className="grid-4">
        <button
          type="button"
          className={`kpi-card ${severityFilter === 'P0' ? 'active' : ''}`}
          onClick={() => { setSeverityFilter((v) => (v === 'P0' ? '' : 'P0')); setPage(1); setTab('findings') }}
        >
          <span className="kpi-label">P0 阻断级隐患（点击筛选）</span>
          <span className="kpi-value" style={{ color: '#b91c1c' }}>{counts.P0}</span>
          <span className="kpi-sub">空值死锁 / 缺 WHERE / 清表并发冲突</span>
        </button>
        <button
          type="button"
          className={`kpi-card ${severityFilter === 'P1' ? 'active' : ''}`}
          onClick={() => { setSeverityFilter((v) => (v === 'P1' ? '' : 'P1')); setPage(1); setTab('findings') }}
        >
          <span className="kpi-label">P1 高优性能与稳定性（点击筛选）</span>
          <span className="kpi-value" style={{ color: '#b45309' }}>{counts.P1}</span>
          <span className="kpi-sub">JOIN 包裹函数 / 重复扫描 / 链过长</span>
        </button>
        <button
          type="button"
          className={`kpi-card ${severityFilter === 'P2' ? 'active' : ''}`}
          onClick={() => { setSeverityFilter((v) => (v === 'P2' ? '' : 'P2')); setPage(1); setTab('findings') }}
        >
          <span className="kpi-label">P2 治理优化建议（点击筛选）</span>
          <span className="kpi-value" style={{ color: '#15803d' }}>{counts.P2}</span>
          <span className="kpi-sub">SELECT * / SYSDATE 分支 / 中心枢纽表</span>
        </button>
        <div className="kpi-card">
          <span className="kpi-label">已治理处置进度</span>
          <span className="kpi-value">
            {counts.resolved} <span style={{ fontSize: 16, color: '#6b7280' }}>/ {counts.total}</span>
          </span>
          <span className="kpi-sub">已采纳或忽略占比 {counts.total ? Math.round((counts.resolved / counts.total) * 100) : 0}%</span>
        </div>
      </div>

      <div className="tabs-bar">
        <button type="button" className={`tab-btn ${tab === 'findings' ? 'active' : ''}`} onClick={() => setTab('findings')}>
          问题排查清单 ({filteredFindings.length})
        </button>
        <button type="button" className={`tab-btn ${tab === 'scripts' ? 'active' : ''}`} onClick={() => setTab('scripts')}>
          按脚本分布汇总 ({scriptSummaryRows.length})
        </button>
        <button type="button" className={`tab-btn ${tab === 'roadmap' ? 'active' : ''}`} onClick={() => setTab('roadmap')}>
          AI 全项目重构路线图 {roadmap?.executive_summary ? '· 已生成' : ''}
        </button>
      </div>

      {tab === 'findings' && (
        <div className="card">
          <div className="filter-bar">
            <div className="filter-chips">
              {['', 'P0', 'P1', 'P2'].map((sev) => (
                <button
                  key={sev || 'ALL'}
                  type="button"
                  className={`filter-chip ${severityFilter === sev ? 'active' : ''}`}
                  onClick={() => { setSeverityFilter(sev); setPage(1) }}
                >
                  {sev || '全部级别'}
                </button>
              ))}
            </div>

            <div className="filter-chips">
              {[['', '全层级'], ['PROJECT', '全局架构'], ['SCRIPT', '单脚本']].map(([sc, lbl]) => (
                <button
                  key={sc || 'ALL'}
                  type="button"
                  className={`filter-chip ${scopeFilter === sc ? 'active' : ''}`}
                  onClick={() => { setScopeFilter(sc); setPage(1) }}
                >
                  {lbl}
                </button>
              ))}
            </div>

            <select className="select" style={{ width: 130 }} value={statusFilter} onChange={(e) => { setStatusFilter(e.target.value); setPage(1) }}>
              <option value="">全部状态</option>
              <option value="OPEN">待处理</option>
              <option value="ADOPTED">已采纳</option>
              <option value="IGNORED">已忽略</option>
            </select>

            <select className="select" style={{ width: 220 }} value={scriptFilter} onChange={(e) => { setScriptFilter(e.target.value); setPage(1) }}>
              <option value="">全部脚本</option>
              {scripts.map((s) => (
                <option key={s.id} value={s.id}>{s.filename}</option>
              ))}
            </select>

            <input
              className="input"
              style={{ flex: 1, minWidth: 180 }}
              placeholder="搜索规则号（如 S001）、表名或描述..."
              value={searchText}
              onChange={(e) => { setSearchText(e.target.value); setPage(1) }}
            />
          </div>

          {paged.items.length === 0 ? (
            <div style={{ padding: 28, textAlign: 'center', color: '#6b7280' }}>
              {allFindings.length === 0
                ? '尚未生成体检结果，请点击右上角「运行离线全仓规则体检」。'
                : '当前筛选条件下无匹配项，可点击上方卡片清除筛选条件。'}
            </div>
          ) : (
            paged.items.map((f) => {
              const isOpen = expandedId === f.id
              const [baseDesc, aiNote] = (f.description || '').split('\n\n[AI 根因诊断]')
              return (
                <div key={f.id} className="finding-row">
                  <div className="finding-row-head" onClick={() => setExpandedId(isOpen ? '' : f.id)}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0, flexWrap: 'wrap' }}>
                      <StatusBadge kind="severity" value={f.severity} />
                      <span className="badge badge-neutral">{f.scope === 'PROJECT' ? '全局' : '脚本'} · {f.rule_code}</span>
                      <strong style={{ fontSize: 13.5 }}>{f.title}</strong>
                      {f.script_filename && (
                        <span style={{ fontSize: 12, color: '#2563eb' }}>
                          {f.script_filename}{f.start_line ? `:${f.start_line}-${f.end_line}` : ''}
                        </span>
                      )}
                      {f.ai_refined && <span className="badge badge-info">AI 已增强</span>}
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
                      <StatusBadge kind="status" value={f.status} />
                      <button type="button" className="btn btn-sm" onClick={(e) => handleMutateStatus(e, f.id, 'ADOPTED')}>采纳</button>
                      <button type="button" className="btn btn-sm" onClick={(e) => handleMutateStatus(e, f.id, 'IGNORED')}>忽略</button>
                      <span style={{ fontSize: 12, color: '#6b7280', marginLeft: 4 }}>{isOpen ? '▲' : '▼'}</span>
                    </div>
                  </div>

                  {isOpen && (
                    <div className="finding-row-body">
                      <div>
                        <strong style={{ fontSize: 12.5, color: '#374151' }}>问题描述</strong>
                        <p style={{ margin: '4px 0 0', fontSize: 13.5 }}>{baseDesc}</p>
                      </div>

                      {aiNote && (
                        <div className="info-callout">
                          <strong>AI 根因诊断：</strong>{aiNote.trim()}
                        </div>
                      )}

                      <div>
                        <strong style={{ fontSize: 12.5, color: '#15803d' }}>修复与优化建议</strong>
                        <p style={{ margin: '4px 0 0', fontSize: 13.5, color: '#1f2937' }}>{f.suggestion}</p>
                      </div>

                      {f.evidence_snippet && (
                        <CodeBlock code={f.evidence_snippet} title="命中证据片段" startLine={f.start_line || 1} collapseAfterLines={12} />
                      )}

                      {f.rewritten_sql && (
                        <CodeBlock code={f.rewritten_sql} title="AI 建议等价优化 SQL（已过语法校验）" collapseAfterLines={16} />
                      )}
                    </div>
                  )}
                </div>
              )
            })
          )}

          <div className="pager">
            <span>第 {paged.page} / {paged.totalPages} 页 · 显示 {paged.items.length} 条（共筛选命中 {paged.total} 条）</span>
            <div style={{ display: 'flex', gap: 6 }}>
              <button type="button" className="btn btn-sm" disabled={paged.page <= 1} onClick={() => setPage((p) => p - 1)}>上一页</button>
              <button type="button" className="btn btn-sm" disabled={paged.page >= paged.totalPages} onClick={() => setPage((p) => p + 1)}>下一页</button>
            </div>
          </div>
        </div>
      )}

      {tab === 'scripts' && (
        <div className="card">
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>脚本文件名</th>
                  <th>类型</th>
                  <th>P0 阻断</th>
                  <th>P1 高优</th>
                  <th>P2 建议</th>
                  <th>合计</th>
                  <th>定向操作</th>
                </tr>
              </thead>
              <tbody>
                {scriptSummaryRows.map((row) => (
                  <tr key={row.id}>
                    <td style={{ fontWeight: 600 }}>{row.filename}</td>
                    <td>{row.file_kind}</td>
                    <td style={{ color: row.P0 ? '#b91c1c' : '#6b7280', fontWeight: row.P0 ? 700 : 400 }}>{row.P0}</td>
                    <td style={{ color: row.P1 ? '#b45309' : '#6b7280', fontWeight: row.P1 ? 600 : 400 }}>{row.P1}</td>
                    <td>{row.P2}</td>
                    <td><strong>{row.total}</strong></td>
                    <td>
                      <div style={{ display: 'flex', gap: 6 }}>
                        <button
                          type="button"
                          className="btn btn-sm"
                          onClick={() => { setScriptFilter(row.id); setScopeFilter('SCRIPT'); setPage(1); setTab('findings') }}
                        >
                          查看问题
                        </button>
                        <button
                          type="button"
                          className="btn btn-sm btn-primary"
                          disabled={busy}
                          onClick={() => handleAnalyzeSingle(row.id, row.filename, true)}
                        >
                          AI 诊断 & 重写
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {tab === 'roadmap' && (
        <div className="card">
          {!roadmap?.executive_summary ? (
            <EmptyState
              title="尚未生成 AI 全项目架构重构路线图"
              description="结合跨脚本血缘、写表冲突与高频热点表，由 AI 统一梳理分阶段重构建议与公共层抽取方案。"
              primaryLabel={busy ? '合成中...' : '立即运行 AI 全仓架构合成'}
              onPrimary={() => handleAnalyzeProject(true)}
            />
          ) : (
            <div>
              <h3 className="card-title">全仓总体架构评估与治理策略</h3>
              <div className="info-callout" style={{ marginBottom: 16 }}>
                {roadmap.executive_summary}
              </div>
              {(roadmap.architecture_recommendations || []).map((rec, idx) => (
                <div key={idx} className="finding-row" style={{ padding: 16 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8, flexWrap: 'wrap' }}>
                    <span className="badge badge-info">{rec.priority}</span>
                    <span className="badge badge-neutral">{rec.category}</span>
                    <strong>{rec.title}</strong>
                  </div>
                  <p style={{ margin: '0 0 8px' }}>{rec.proposal}</p>
                  <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', fontSize: 12.5, color: '#15803d' }}>
                    <span>预期收益：{rec.expected_benefit}</span>
                    {(rec.affected_tables || []).map((t) => (
                      <code key={t} style={{ background: '#f3f4f6', padding: '1px 6px', borderRadius: 4, color: '#1f2937' }}>{t}</code>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
