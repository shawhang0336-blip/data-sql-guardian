import React, { useEffect, useState } from 'react'
import { PageHeader } from '../../components/PageHeader.jsx'
import { StatusBadge } from '../../components/StatusBadge.jsx'
import { useProjectContext } from '../../context/ProjectContext.jsx'
import { useAsync } from '../../hooks/useAsync.js'
import { getProjectDetail } from '../../api/workspace.js'
import {
  listFindings,
  runProjectAnalysis,
  runScriptAnalysis,
  updateFindingStatus,
} from '../../api/analysis.js'

export default function AnalysisPage() {
  const { selectedProjectId, refreshProjects } = useProjectContext()
  const { busy, error, setError, run } = useAsync()

  const [scripts, setScripts] = useState([])
  const [selectedScriptId, setSelectedScriptId] = useState('')
  const [scopeFilter, setScopeFilter] = useState('')
  const [severityFilter, setSeverityFilter] = useState('')
  const [findings, setFindings] = useState([])
  const [roadmap, setRoadmap] = useState(null)
  const [runLlm, setRunLlm] = useState(false)
  const [notice, setNotice] = useState('')

  const loadData = async (pid) => {
    if (!pid) {
      setScripts([])
      setFindings([])
      return
    }
    const [proj, fRes] = await Promise.all([
      getProjectDetail(pid),
      listFindings(pid, {
        scope: scopeFilter,
        severity: severityFilter,
        script_id: scopeFilter === 'SCRIPT' ? selectedScriptId : '',
      }),
    ])
    setScripts(proj?.scripts || [])
    setRoadmap(proj?.summary?.ai_roadmap || null)
    setFindings(fRes?.findings || [])
    if (!selectedScriptId && proj?.scripts?.length) {
      setSelectedScriptId(proj.scripts[0].id)
    }
  }

  useEffect(() => {
    loadData(selectedProjectId).catch((e) => setError(e?.message || '加载分析结果失败'))
  }, [selectedProjectId, scopeFilter, severityFilter])

  const handleAnalyzeProject = () =>
    run(async () => {
      const res = await runProjectAnalysis(selectedProjectId, { run_llm: runLlm })
      setNotice(`全项目分析完成：单脚本规则命中 ${res.script_findings} 条，全局架构规则命中 ${res.project_findings} 条（AI 状态：${res.ai_status}）。`)
      await loadData(selectedProjectId)
      await refreshProjects(selectedProjectId)
    })

  const handleAnalyzeSingle = () => {
    if (!selectedScriptId) return setError('请先选择一个脚本')
    run(async () => {
      const res = await runScriptAnalysis(selectedProjectId, selectedScriptId, { run_llm: runLlm })
      setNotice(`脚本 \`${res.filename}\` 剖析完成：发现 ${res.finding_count} 条问题（AI 状态：${res.ai_status}）。`)
      await loadData(selectedProjectId)
      await refreshProjects(selectedProjectId)
    })
  }

  const handleMutateStatus = (fid, status) =>
    run(async () => {
      await updateFindingStatus(fid, status)
      await loadData(selectedProjectId)
    })

  return (
    <div>
      <PageHeader
        title="单脚本深度剖析 & 全项目全局优化"
        subtitle="16 条单脚本确定性静态规则（精准识别 NULL 等值死锁/函数包裹连接/动态 SQL/SYS_GUID 等）+ 6 条全仓血缘冲突与重复逻辑聚类规则"
        actions={
          <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 13 }}>
              <input type="checkbox" checked={runLlm} onChange={(e) => setRunLlm(e.target.checked)} />
              启用 AI 根因诊断与 SQL 重写
            </label>
            <button type="button" className="btn btn-primary" disabled={busy || !selectedProjectId} onClick={handleAnalyzeProject}>
              {busy ? '扫描中...' : '全项目静态 + 全局优化扫描'}
            </button>
          </div>
        }
      />

      {error && <div className="alert alert-error">{error}</div>}
      {notice && <div className="alert alert-info">{notice}</div>}

      <div className="card" style={{ display: 'flex', gap: 12, flexWrap: 'wrap', alignItems: 'center' }}>
        <select className="select" style={{ width: 260 }} value={selectedScriptId} onChange={(e) => setSelectedScriptId(e.target.value)}>
          <option value="">选择单脚本进行剖析...</option>
          {scripts.map((s) => (
            <option key={s.id} value={s.id}>
              {s.filename} ({s.finding_count ?? 0} 条问题)
            </option>
          ))}
        </select>
        <button type="button" className="btn" disabled={busy || !selectedScriptId} onClick={handleAnalyzeSingle}>
          单脚本定向剖析
        </button>

        <select className="select" style={{ width: 150 }} value={scopeFilter} onChange={(e) => setScopeFilter(e.target.value)}>
          <option value="">全部层级</option>
          <option value="PROJECT">PROJECT 全局级</option>
          <option value="SCRIPT">SCRIPT 单脚本级</option>
        </select>

        <select className="select" style={{ width: 140 }} value={severityFilter} onChange={(e) => setSeverityFilter(e.target.value)}>
          <option value="">全部级别</option>
          <option value="P0">P0 阻断</option>
          <option value="P1">P1 高优</option>
          <option value="P2">P2 建议</option>
        </select>
      </div>

      {roadmap && roadmap.executive_summary && (
        <div className="card" style={{ borderLeft: '4px solid #2563eb' }}>
          <h3 style={{ marginTop: 0 }}>AI 全项目架构重构总结</h3>
          <p style={{ margin: '4px 0 10px' }}>{roadmap.executive_summary}</p>
          {(roadmap.architecture_recommendations || []).map((rec, idx) => (
            <div key={idx} style={{ padding: '8px 0', borderTop: '1px solid #e2e8f0' }}>
              <strong>[{rec.priority}] {rec.title}</strong> — {rec.proposal}
              <div style={{ fontSize: 12, color: '#16a34a' }}>预期收益：{rec.expected_benefit}</div>
            </div>
          ))}
        </div>
      )}

      <div className="card">
        <h3 style={{ marginTop: 0 }}>优化问题清单（共 {findings.length} 条）</h3>
        {findings.length === 0 ? (
          <div style={{ color: '#64748b' }}>暂无记录，请点击右上角「全项目静态 + 全局优化扫描」。</div>
        ) : (
          findings.map((f) => (
            <div key={f.id} style={{ border: '1px solid #e2e8f0', borderRadius: 8, padding: 14, marginBottom: 12 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                  <StatusBadge kind="severity" value={f.severity} />
                  <span className="badge badge-neutral">{f.scope} · {f.rule_code}</span>
                  <strong>{f.title}</strong>
                  {f.script_filename && <span style={{ color: '#2563eb', fontSize: 12 }}>{f.script_filename}:{f.start_line}-{f.end_line}</span>}
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  <StatusBadge kind="status" value={f.status} />
                  <button type="button" className="btn" onClick={() => handleMutateStatus(f.id, 'ADOPTED')}>采纳</button>
                  <button type="button" className="btn" onClick={() => handleMutateStatus(f.id, 'IGNORED')}>忽略</button>
                </div>
              </div>
              <p style={{ margin: '8px 0 6px', whiteSpace: 'pre-wrap' }}>{f.description}</p>
              <div style={{ fontSize: 12, color: '#0f766e', marginBottom: 8 }}>优化建议：{f.suggestion}</div>
              {f.evidence_snippet && <pre className="code-block" style={{ marginBottom: f.rewritten_sql ? 8 : 0 }}>{f.evidence_snippet}</pre>}
              {f.rewritten_sql && (
                <div>
                  <div style={{ fontSize: 12, fontWeight: 600, color: '#16a34a', marginBottom: 4 }}>AI 建议等价重写 SQL：</div>
                  <pre className="code-block">{f.rewritten_sql}</pre>
                </div>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  )
}
