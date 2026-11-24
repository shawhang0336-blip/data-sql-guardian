import React, { useEffect, useState } from 'react'
import { PageHeader } from '../../components/PageHeader.jsx'
import { StatusBadge } from '../../components/StatusBadge.jsx'
import { CodeDiffView } from '../../components/CodeDiffView.jsx'
import { CodeBlock } from '../../components/CodeBlock.jsx'
import { EmptyState } from '../../components/EmptyState.jsx'
import { useProjectContext } from '../../context/ProjectContext.jsx'
import { useAsync } from '../../hooks/useAsync.js'
import { getProjectDetail, getScriptDetail } from '../../api/workspace.js'
import {
  approveChangeReview,
  createChangeReview,
  exportReviewMarkdown,
  getChangeReviewDetail,
  getRunTrace,
  listChangeReviews,
  rejectChangeReview,
} from '../../api/review.js'
import { formatUtcTime } from '../../utils/labels.js'

export default function ReviewPage() {
  const { selectedProjectId, refreshProjects, setImportModalOpen, pushToast } = useProjectContext()
  const { busy, error, setError, run } = useAsync()

  const [scripts, setScripts] = useState([])
  const [reviews, setReviews] = useState([])
  const [selectedReview, setSelectedReview] = useState(null)
  const [creatingNew, setCreatingNew] = useState(false)
  const [detailTab, setDetailTab] = useState('blast') // blast | diff | findings | report
  const [trace, setTrace] = useState(null)
  const [exportedMd, setExportedMd] = useState('')

  const [changeType, setChangeType] = useState('MODIFY')
  const [targetScriptId, setTargetScriptId] = useState('')
  const [filename, setFilename] = useState('SP_NEW_ETL_TASK.sql')
  const [author, setAuthor] = useState('数据研发工程师')
  const [changeSummary, setChangeSummary] = useState('修复空值关联与性能改造')
  const [newContent, setNewContent] = useState('')
  const [runLlm, setRunLlm] = useState(false)

  const loadData = async (pid, preferReviewId) => {
    if (!pid) return
    const [proj, revRes] = await Promise.all([
      getProjectDetail(pid),
      listChangeReviews(pid),
    ])
    const sList = proj?.scripts || []
    setScripts(sList)
    if (sList.length && !targetScriptId) setTargetScriptId(sList[0].id)

    const rList = revRes?.reviews || []
    setReviews(rList)
    const targetId = preferReviewId || selectedReview?.id || rList[0]?.id
    if (targetId && rList.some((r) => r.id === targetId)) {
      setSelectedReview(await getChangeReviewDetail(targetId))
      setCreatingNew(false)
    } else if (rList.length === 0) {
      setSelectedReview(null)
      setCreatingNew(true)
    }
  }

  useEffect(() => {
    loadData(selectedProjectId).catch((e) => setError(e?.message || '加载变更评估列表失败'))
  }, [selectedProjectId])

  const handleLoadBaseline = () => {
    if (!targetScriptId) return
    run(async () => {
      const sd = await getScriptDetail(selectedProjectId, targetScriptId)
      setFilename(sd.filename)
      setNewContent(sd.content || '')
      pushToast('info', `已载入 ${sd.filename} 当前基线版本 v${sd.current_version}`)
    })
  }

  const handleSubmitReview = (e) => {
    e.preventDefault()
    if (!newContent.trim()) return setError('请先填写或载入待评估的完整 SQL 代码')
    run(async () => {
      const created = await createChangeReview(selectedProjectId, {
        change_type: changeType,
        filename: filename.trim(),
        target_script_id: changeType === 'MODIFY' ? targetScriptId : null,
        new_content: newContent,
        author: author.trim() || '评审工程师',
        change_summary: changeSummary.trim(),
        run_llm: runLlm,
      })
      pushToast('info', `评估完成：门禁裁决 ${created.final_verdict}，波及 ${created.blast_radius?.total_impacted_scripts || 0} 个下游脚本`)
      setDetailTab('blast')
      setTrace(null)
      setExportedMd('')
      await loadData(selectedProjectId, created.id)
      await refreshProjects(selectedProjectId)
    })
  }

  const handleSelectReview = (rid) =>
    run(async () => {
      setCreatingNew(false)
      setExportedMd('')
      setTrace(null)
      setSelectedReview(await getChangeReviewDetail(rid))
    })

  const handleApprove = (mergeBaseline) =>
    run(async () => {
      await approveChangeReview(selectedReview.id, {
        reviewer: author.trim() || '评审工程师',
        comment: '审核通过并同意合并基线',
        merge_baseline: mergeBaseline,
      })
      pushToast('success', mergeBaseline ? '已批准并合并入基线（版本号 +1，血缘已重建）' : '已批准上线')
      await loadData(selectedProjectId, selectedReview.id)
      await refreshProjects(selectedProjectId)
    })

  const handleReject = () =>
    run(async () => {
      await rejectChangeReview(selectedReview.id, {
        reviewer: author.trim() || '评审工程师',
        comment: '触发阻断或风险过高，退回修改',
      })
      pushToast('info', '已驳回变更单，原基线保持未动')
      await loadData(selectedProjectId, selectedReview.id)
    })

  const handleOpenReportTab = (rid, runId) => {
    setDetailTab('report')
    run(async () => {
      const [mdRes, trRes] = await Promise.all([exportReviewMarkdown(rid), getRunTrace(runId)])
      setExportedMd(mdRes.markdown || '')
      setTrace(trRes)
    })
  }

  if (!selectedProjectId) {
    return (
      <EmptyState
        title="请先选择或导入 SQL 项目"
        description="导入工程基线后，可提交 ADD / MODIFY 脚本变更进行 AST Diff、多跳爆破半径分析与上线前红线门禁评估。"
        primaryLabel="+ 导入项目"
        onPrimary={() => setImportModalOpen(true)}
      />
    )
  }

  const diff = selectedReview?.diff_summary || {}
  const blast = selectedReview?.blast_radius || {}
  const ai = selectedReview?.ai_review || {}

  return (
    <div>
      <PageHeader
        title="上线前变更评估与发布门禁"
        subtitle="提交新增或修改脚本 -> 自动核算 AST 结构差异与多跳下游影响 -> 强制红线门禁 -> Interrupt 审批合入基线"
        actions={
          <button type="button" className="btn btn-primary" onClick={() => { setCreatingNew(true); setError('') }}>
            + 新建变更评估
          </button>
        }
      />

      {error && <div className="alert alert-error">{error}</div>}

      <div className="split-pane">
        <div className="master-list">
          <div className="master-list-head">
            <strong style={{ fontSize: 13 }}>历史变更评估单 ({reviews.length})</strong>
          </div>
          <div className="master-list-scroll">
            {reviews.length === 0 ? (
              <div style={{ padding: 16, fontSize: 12.5, color: '#6b7280' }}>暂无变更单，请在右侧提交首个待评估变更。</div>
            ) : (
              reviews.map((r) => (
                <button
                  type="button"
                  key={r.id}
                  className={`master-item verdict-${r.final_verdict} ${!creatingNew && selectedReview?.id === r.id ? 'active' : ''}`}
                  onClick={() => handleSelectReview(r.id)}
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 6 }}>
                    <strong style={{ fontSize: 13, wordBreak: 'break-all' }}>{r.filename}</strong>
                    <StatusBadge kind="verdict" value={r.final_verdict} />
                  </div>
                  <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11.5, color: '#6b7280' }}>
                    <span>{r.change_type} · 下游 {r.blast_radius?.total_impacted_scripts ?? 0}</span>
                    <span>{formatUtcTime(r.created_at)}</span>
                  </div>
                </button>
              ))
            )}
          </div>
        </div>

        <div className="detail-pane">
          {creatingNew || !selectedReview ? (
            <form onSubmit={handleSubmitReview}>
              <h2 style={{ marginTop: 0, fontSize: 17 }}>提交待评估脚本变更</h2>

              <div className="filter-bar">
                <div className="tabs-bar" style={{ marginBottom: 0 }}>
                  <button type="button" className={`tab-btn ${changeType === 'MODIFY' ? 'active' : ''}`} onClick={() => setChangeType('MODIFY')}>
                    MODIFY · 修改基线脚本
                  </button>
                  <button type="button" className={`tab-btn ${changeType === 'ADD' ? 'active' : ''}`} onClick={() => setChangeType('ADD')}>
                    ADD · 新增脚本
                  </button>
                </div>

                {changeType === 'MODIFY' ? (
                  <>
                    <select className="select" style={{ width: 250 }} value={targetScriptId} onChange={(e) => setTargetScriptId(e.target.value)}>
                      {scripts.map((s) => (
                        <option key={s.id} value={s.id}>{s.filename}</option>
                      ))}
                    </select>
                    <button type="button" className="btn" onClick={handleLoadBaseline}>
                      载入基线源码编辑
                    </button>
                  </>
                ) : (
                  <input className="input" style={{ width: 260 }} value={filename} onChange={(e) => setFilename(e.target.value)} placeholder="新增脚本名.sql" />
                )}
              </div>

              <div className="grid-2" style={{ marginBottom: 12 }}>
                <label>
                  <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>提交人</div>
                  <input className="input" value={author} onChange={(e) => setAuthor(e.target.value)} />
                </label>
                <label>
                  <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>变更说明</div>
                  <input className="input" value={changeSummary} onChange={(e) => setChangeSummary(e.target.value)} />
                </label>
              </div>

              <label style={{ display: 'block', marginBottom: 12 }}>
                <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>新版本 SQL / 存储过程内容</div>
                <textarea
                  className="textarea"
                  rows={12}
                  value={newContent}
                  onChange={(e) => setNewContent(e.target.value)}
                  placeholder="在此输入修改或新增的完整 SQL 内容..."
                />
              </label>

              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 12 }}>
                <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 13 }}>
                  <input type="checkbox" checked={runLlm} onChange={(e) => setRunLlm(e.target.checked)} />
                  同步执行 AI 风险审计与验证回滚 SQL 生成
                </label>
                <button type="submit" className="btn btn-primary" disabled={busy}>
                  {busy ? '正在运行门禁与爆破推演...' : '运行上线前门禁评估'}
                </button>
              </div>
            </form>
          ) : (
            <div>
              <div className={`verdict-banner ${selectedReview.final_verdict}`}>
                <div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                    <StatusBadge kind="verdict" value={selectedReview.final_verdict} />
                    <StatusBadge kind="status" value={selectedReview.status} />
                    <strong style={{ fontSize: 16 }}>{selectedReview.filename}</strong>
                  </div>
                  <div style={{ fontSize: 13, marginTop: 6, color: '#374151' }}>
                    确定性门禁：{selectedReview.deterministic_verdict} · 代码变动 +{diff.added_lines || 0} / -{diff.removed_lines || 0} 行 · 下游影响 {blast.total_impacted_scripts || 0} 个脚本（最深 {blast.max_hop || 0} 跳）
                  </div>
                  {(diff.block_reasons || []).map((r, i) => (
                    <div key={i} style={{ fontSize: 12.5, color: '#b91c1c', marginTop: 4 }}>• [阻断] {r}</div>
                  ))}
                  {(diff.warn_reasons || []).map((r, i) => (
                    <div key={i} style={{ fontSize: 12.5, color: '#b45309', marginTop: 4 }}>• [告警] {r}</div>
                  ))}
                </div>

                {selectedReview.status === 'WAITING_APPROVAL' && (
                  <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                    <button type="button" className="btn btn-ok" disabled={busy} onClick={() => handleApprove(true)}>
                      批准并合并入基线
                    </button>
                    <button type="button" className="btn btn-danger" disabled={busy} onClick={handleReject}>
                      驳回变更
                    </button>
                  </div>
                )}
              </div>

              <div className="tabs-bar">
                <button type="button" className={`tab-btn ${detailTab === 'blast' ? 'active' : ''}`} onClick={() => setDetailTab('blast')}>
                  爆破半径 ({blast.total_impacted_scripts || 0})
                </button>
                <button type="button" className={`tab-btn ${detailTab === 'diff' ? 'active' : ''}`} onClick={() => setDetailTab('diff')}>
                  Unified Diff 差异
                </button>
                <button type="button" className={`tab-btn ${detailTab === 'findings' ? 'active' : ''}`} onClick={() => setDetailTab('findings')}>
                  增量告警 & AI 评估 ({(selectedReview.new_findings || []).length})
                </button>
                <button
                  type="button"
                  className={`tab-btn ${detailTab === 'report' ? 'active' : ''}`}
                  onClick={() => handleOpenReportTab(selectedReview.id, selectedReview.run_id)}
                >
                  导出报告 & 哈希链
                </button>
              </div>

              {detailTab === 'blast' && (
                <div>
                  {(blast.impacted_scripts || []).length === 0 ? (
                    <div style={{ padding: 20, background: '#f9fafb', borderRadius: 8, color: '#6b7280' }}>
                      当前变更未影响其他下游脚本。
                    </div>
                  ) : (
                    <div className="table-wrap">
                      <table className="table">
                        <thead>
                          <tr><th>传导层级</th><th>下游受影响脚本</th><th>经过表</th><th>依赖类型</th></tr>
                        </thead>
                        <tbody>
                          {blast.impacted_scripts.map((s, i) => (
                            <tr key={i}>
                              <td><span className={`badge ${s.hop === 1 ? 'badge-warn' : 'badge-neutral'}`}>H{s.hop}</span></td>
                              <td style={{ fontWeight: 600 }}>{s.filename}</td>
                              <td><code>{s.via_table}</code></td>
                              <td>{s.edge_kind}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              )}

              {detailTab === 'diff' && <CodeDiffView diffText={diff.unified_diff} />}

              {detailTab === 'findings' && (
                <div style={{ display: 'grid', gap: 12 }}>
                  {(selectedReview.new_findings || []).map((f, idx) => (
                    <div key={idx} className="finding-row" style={{ padding: 14 }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                        <StatusBadge kind="severity" value={f.severity} />
                        <span className="badge badge-neutral">{f.rule_code}</span>
                        <strong>{f.title}</strong>
                      </div>
                      <p style={{ margin: '0 0 6px', fontSize: 13 }}>{f.description}</p>
                      <div style={{ fontSize: 12.5, color: '#15803d' }}>建议：{f.suggestion}</div>
                    </div>
                  ))}
                  {ai.ai_status === 'COMPLETED' && (
                    <div className="info-callout">
                      <h4 style={{ margin: '0 0 6px' }}>AI 上线前综合风险评估与核对预案</h4>
                      <div><strong>风险概要：</strong>{ai.risk_summary}</div>
                      <div><strong>研发建议：</strong>{ai.developer_notes}</div>
                      <div><strong>DBA 建议：</strong>{ai.dba_notes}</div>
                      <div><strong>回滚预案：</strong>{ai.rollback_plan}</div>
                    </div>
                  )}
                </div>
              )}

              {detailTab === 'report' && (
                <div style={{ display: 'grid', gap: 14 }}>
                  {trace && (
                    <div className="info-callout" style={{ background: trace.chain_verified ? '#f0fdf4' : '#fef2f2', borderColor: trace.chain_verified ? '#bbf7d0' : '#fecaca', color: '#111827' }}>
                      <strong>EventLog 防篡改 SHA-256 哈希链校验：</strong>
                      {trace.chain_verified ? '校验通过（完整未篡改）' : '链断裂异常'} · 共 {trace.event_count} 条账本事件
                      <div style={{ fontSize: 12, marginTop: 6, fontFamily: 'monospace' }}>
                        {trace.events.map((e) => `#${e.seq} [${e.kind}] @${e.node}`).join('  →  ')}
                      </div>
                    </div>
                  )}
                  <CodeBlock code={exportedMd || '正在加载 Markdown 评估报告...'} title={`${selectedReview.filename} 评估报告 (.md)`} collapseAfterLines={0} maxHeight={460} />
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
