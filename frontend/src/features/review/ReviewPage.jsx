import React, { useEffect, useState } from 'react'
import { PageHeader } from '../../components/PageHeader.jsx'
import { StatusBadge } from '../../components/StatusBadge.jsx'
import { CodeDiffView } from '../../components/CodeDiffView.jsx'
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
  const { selectedProjectId, refreshProjects } = useProjectContext()
  const { busy, error, setError, run } = useAsync()

  const [scripts, setScripts] = useState([])
  const [reviews, setReviews] = useState([])
  const [selectedReview, setSelectedReview] = useState(null)
  const [trace, setTrace] = useState(null)
  const [exportedMd, setExportedMd] = useState('')

  const [changeType, setChangeType] = useState('MODIFY')
  const [targetScriptId, setTargetScriptId] = useState('')
  const [filename, setFilename] = useState('SP_NEW_ETL_TASK.sql')
  const [author, setAuthor] = useState('数据研发工程师')
  const [changeSummary, setChangeSummary] = useState('修复空值关联与性能优化')
  const [newContent, setNewContent] = useState('')
  const [runLlm, setRunLlm] = useState(false)
  const [notice, setNotice] = useState('')

  const loadData = async (pid) => {
    if (!pid) return
    const [proj, revRes] = await Promise.all([
      getProjectDetail(pid),
      listChangeReviews(pid),
    ])
    const sList = proj?.scripts || []
    setScripts(sList)
    const rList = revRes?.reviews || []
    setReviews(rList)
    if (rList.length) {
      const detail = await getChangeReviewDetail(rList[0].id)
      setSelectedReview(detail)
    } else {
      setSelectedReview(null)
    }
    if (sList.length && !targetScriptId) {
      setTargetScriptId(sList[0].id)
    }
  }

  useEffect(() => {
    loadData(selectedProjectId).catch((e) => setError(e?.message || '加载评审单失败'))
  }, [selectedProjectId])

  const handleLoadBaselineIntoEditor = () => {
    if (!targetScriptId) return
    run(async () => {
      const sd = await getScriptDetail(selectedProjectId, targetScriptId)
      setFilename(sd.filename)
      setNewContent(sd.content || '')
    })
  }

  const handleSubmitReview = (e) => {
    e.preventDefault()
    if (!newContent.trim()) return setError('请输入待评估 SQL 脚本内容')
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
      setNotice(`评估完成！门禁裁决：${created.final_verdict}（下游波及 ${created.blast_radius?.total_impacted_scripts || 0} 个脚本）`)
      const detail = await getChangeReviewDetail(created.id)
      setSelectedReview(detail)
      const revRes = await listChangeReviews(selectedProjectId)
      setReviews(revRes?.reviews || [])
      await refreshProjects(selectedProjectId)
    })
  }

  const handleSelectReview = (rid) =>
    run(async () => {
      setExportedMd('')
      setTrace(null)
      setSelectedReview(await getChangeReviewDetail(rid))
    })

  const handleApprove = (mergeBaseline) =>
    run(async () => {
      await approveChangeReview(selectedReview.id, {
        reviewer: author.trim() || '评审工程师',
        comment: '核查确认后批准上线',
        merge_baseline: mergeBaseline,
      })
      setNotice(mergeBaseline ? '已批准上线并合并入项目基线（自动重算血缘与版本+1）！' : '已批准上线（未合并基线）。')
      await loadData(selectedProjectId)
      await refreshProjects(selectedProjectId)
    })

  const handleReject = () =>
    run(async () => {
      await rejectChangeReview(selectedReview.id, {
        reviewer: author.trim() || '评审工程师',
        comment: '存在未消除风险，退回修改',
      })
      setNotice('已驳回变更单，生产基线保持不变。')
      await loadData(selectedProjectId)
    })

  const handleExportMd = () =>
    run(async () => {
      const res = await exportReviewMarkdown(selectedReview.id)
      setExportedMd(res.markdown || '')
    })

  const handleLoadTrace = () =>
    run(async () => {
      const res = await getRunTrace(selectedReview.run_id)
      setTrace(res)
    })

  return (
    <div>
      <PageHeader
        title="上线前脚本变更评估与发布门禁"
        subtitle="提交 ADD / MODIFY 脚本变更 -> AST Diff 与多跳下游爆破半径推演 -> 确定性 BLOCK/WARN/PASS 门禁 -> Interrupt 人工审批合并基线"
      />

      {error && <div className="alert alert-error">{error}</div>}
      {notice && <div className="alert alert-info">{notice}</div>}

      <div className="card">
        <h3 style={{ marginTop: 0 }}>提交变更脚本评估</h3>
        <form onSubmit={handleSubmitReview}>
          <div style={{ display: 'flex', gap: 10, marginBottom: 10, flexWrap: 'wrap' }}>
            <select className="select" style={{ width: 140 }} value={changeType} onChange={(e) => setChangeType(e.target.value)}>
              <option value="MODIFY">MODIFY 修改基线脚本</option>
              <option value="ADD">ADD 新增脚本</option>
            </select>

            {changeType === 'MODIFY' ? (
              <>
                <select className="select" style={{ width: 240 }} value={targetScriptId} onChange={(e) => setTargetScriptId(e.target.value)}>
                  {scripts.map((s) => (
                    <option key={s.id} value={s.id}>{s.filename}</option>
                  ))}
                </select>
                <button type="button" className="btn" onClick={handleLoadBaselineIntoEditor}>
                  载入当前基线源码到编辑框
                </button>
              </>
            ) : (
              <input className="input" style={{ width: 240 }} value={filename} onChange={(e) => setFilename(e.target.value)} placeholder="新增文件名.sql" />
            )}

            <input className="input" style={{ width: 150 }} value={author} onChange={(e) => setAuthor(e.target.value)} placeholder="提交人" />
            <input className="input" style={{ flex: 1, minWidth: 180 }} value={changeSummary} onChange={(e) => setChangeSummary(e.target.value)} placeholder="变更说明" />
            <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 13 }}>
              <input type="checkbox" checked={runLlm} onChange={(e) => setRunLlm(e.target.checked)} />
              AI 风险与核对 SQL 评审
            </label>
            <button type="submit" className="btn btn-primary" disabled={busy || !selectedProjectId}>
              {busy ? '评估推演中...' : '运行上线前门禁评估'}
            </button>
          </div>
          <textarea
            className="textarea"
            rows={7}
            value={newContent}
            onChange={(e) => setNewContent(e.target.value)}
            placeholder="在此粘贴新增或修改后的完整 SQL / 存储过程内容..."
          />
        </form>
      </div>

      <div className="grid-2">
        <div className="card">
          <h3 style={{ marginTop: 0 }}>历史变更评估单</h3>
          <table className="table">
            <thead>
              <tr>
                <th>脚本 / 类型</th>
                <th>门禁裁决</th>
                <th>审批状态</th>
                <th>下游影响</th>
              </tr>
            </thead>
            <tbody>
              {reviews.map((r) => (
                <tr
                  key={r.id}
                  onClick={() => handleSelectReview(r.id)}
                  style={{ cursor: 'pointer', background: selectedReview?.id === r.id ? '#eff6ff' : 'transparent' }}
                >
                  <td>
                    <div style={{ fontWeight: 600 }}>{r.filename}</div>
                    <div style={{ fontSize: 11, color: '#64748b' }}>{r.change_type} · {formatUtcTime(r.created_at)}</div>
                  </td>
                  <td><StatusBadge kind="verdict" value={r.final_verdict} /></td>
                  <td><StatusBadge kind="status" value={r.status} /></td>
                  <td>{r.blast_radius?.total_impacted_scripts ?? 0} 个脚本</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="card">
          {selectedReview ? (
            <div>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 8 }}>
                <h3 style={{ margin: 0 }}>{selectedReview.filename} 评估详情</h3>
                <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                  <StatusBadge kind="verdict" value={selectedReview.final_verdict} />
                  <StatusBadge kind="status" value={selectedReview.status} />
                </div>
              </div>

              <div style={{ fontSize: 12, color: '#475569', margin: '8px 0 12px' }}>
                确定性裁决：{selectedReview.deterministic_verdict} · 代码变动 +{selectedReview.diff_summary?.added_lines || 0} / -{selectedReview.diff_summary?.removed_lines || 0} 行 · 下游影响 {selectedReview.blast_radius?.total_impacted_scripts || 0} 个脚本（{selectedReview.blast_radius?.max_hop || 0} 跳）
              </div>

              {(selectedReview.diff_summary?.block_reasons || []).map((b, i) => (
                <div key={i} className="alert alert-error">[BLOCK] {b}</div>
              ))}
              {(selectedReview.diff_summary?.warn_reasons || []).map((w, i) => (
                <div key={i} className="alert alert-info">[WARN] {w}</div>
              ))}

              <div style={{ display: 'flex', gap: 8, marginBottom: 14, flexWrap: 'wrap' }}>
                {selectedReview.status === 'WAITING_APPROVAL' && (
                  <>
                    <button type="button" className="btn btn-ok" disabled={busy} onClick={() => handleApprove(true)}>
                      批准上线并合并入基线
                    </button>
                    <button type="button" className="btn btn-danger" disabled={busy} onClick={handleReject}>
                      驳回变更
                    </button>
                  </>
                )}
                <button type="button" className="btn" onClick={handleExportMd}>导出 Markdown 评估报告</button>
                <button type="button" className="btn" onClick={handleLoadTrace}>查看防篡改哈希链路</button>
              </div>

              <h4>1. 下游多跳爆破半径</h4>
              {(selectedReview.blast_radius?.impacted_scripts || []).length === 0 ? (
                <div style={{ fontSize: 12, color: '#64748b', marginBottom: 10 }}>未发现下游依赖脚本。</div>
              ) : (
                <table className="table" style={{ marginBottom: 12 }}>
                  <thead><tr><th>跳数</th><th>下游脚本</th><th>中间表</th><th>依赖</th></tr></thead>
                  <tbody>
                    {selectedReview.blast_radius.impacted_scripts.map((s, idx) => (
                      <tr key={idx}><td>H{s.hop}</td><td>{s.filename}</td><td><code>{s.via_table}</code></td><td>{s.edge_kind}</td></tr>
                    ))}
                  </tbody>
                </table>
              )}

              <h4>2. Unified Diff 代码差异</h4>
              <CodeDiffView diffText={selectedReview.diff_summary?.unified_diff} />

              {exportedMd && (
                <div style={{ marginTop: 12 }}>
                  <h4>Markdown 报告预览</h4>
                  <pre className="code-block">{exportedMd}</pre>
                </div>
              )}

              {trace && (
                <div style={{ marginTop: 12 }}>
                  <h4>SHA-256 事件溯源链（验签：{trace.chain_verified ? 'PASS 完整未篡改' : 'BROKEN'}）</h4>
                  <pre className="code-block">
                    {trace.events.map((e) => `#${e.seq} [${e.kind}] @${e.node} -> ${e.event_hash.slice(0, 16)}...`).join('\n')}
                  </pre>
                </div>
              )}
            </div>
          ) : (
            <div style={{ color: '#64748b' }}>请选择或提交一条变更单查看详情。</div>
          )}
        </div>
      </div>
    </div>
  )
}
