import React, { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { PageHeader } from '../../components/PageHeader.jsx'
import { EmptyState } from '../../components/EmptyState.jsx'
import { CodeBlock } from '../../components/CodeBlock.jsx'
import { useProjectContext } from '../../context/ProjectContext.jsx'
import { useAsync } from '../../hooks/useAsync.js'
import { paginateItems } from '../../utils/labels.js'
import {
  deleteProject,
  getProjectDetail,
  getProjectLineage,
  getScriptDetail,
  importSacProject,
  listProjectSchemas,
  rebuildProjectLineage,
} from '../../api/workspace.js'

export default function WorkspacePage() {
  const navigate = useNavigate()
  const {
    selectedProjectId,
    refreshProjects,
    setImportModalOpen,
    pushToast,
  } = useProjectContext()
  const { busy, error, setError, run } = useAsync()

  const [tab, setTab] = useState('scripts') // scripts | lineage | schemas
  const [detail, setDetail] = useState(null)
  const [lineage, setLineage] = useState(null)
  const [schemas, setSchemas] = useState([])
  const [selectedScript, setSelectedScript] = useState(null)

  const [scriptSearch, setScriptSearch] = useState('')
  const [chunkMode, setChunkMode] = useState('chunks') // chunks | full
  const [chunkSearch, setChunkSearch] = useState('')
  const [edgeKindFilter, setEdgeKindFilter] = useState('ALL')
  const [edgeSearch, setEdgeSearch] = useState('')
  const [edgePage, setEdgePage] = useState(1)
  const [schemaSearch, setSchemaSearch] = useState('')
  const [expandedSchemaId, setExpandedSchemaId] = useState('')

  const loadWorkspaceData = async (pid) => {
    if (!pid) {
      setDetail(null)
      setLineage(null)
      setSchemas([])
      setSelectedScript(null)
      return
    }
    const [d, lin, sch] = await Promise.all([
      getProjectDetail(pid),
      getProjectLineage(pid),
      listProjectSchemas(pid),
    ])
    setDetail(d)
    setLineage(lin)
    setSchemas(sch?.schemas || [])
    if (d?.scripts?.length) {
      const sd = await getScriptDetail(pid, d.scripts[0].id)
      setSelectedScript(sd)
    } else {
      setSelectedScript(null)
    }
  }

  useEffect(() => {
    loadWorkspaceData(selectedProjectId).catch((e) => setError(e?.message || '加载项目工作台失败'))
  }, [selectedProjectId])

  const handleQuickSac = () =>
    run(async () => {
      const created = await importSacProject('SAC 真实 Oracle ETL 资产库')
      pushToast('success', `已导入 SAC 资产库：${created.script_count} 个脚本，${created.chunk_count} 个语句切块`)
      await refreshProjects(created.id)
    })

  const handleSelectScript = (sid) =>
    run(async () => {
      const sd = await getScriptDetail(selectedProjectId, sid)
      setSelectedScript(sd)
    })

  const handleRebuildLineage = () =>
    run(async () => {
      const lin = await rebuildProjectLineage(selectedProjectId)
      setLineage(lin)
      pushToast('info', `已重算跨脚本血缘：${lin?.edges?.length || 0} 条依赖边`)
    })

  const handleDeleteProject = () =>
    run(async () => {
      if (!selectedProjectId) return
      await deleteProject(selectedProjectId)
      pushToast('info', '已删除当前项目')
      await refreshProjects('')
    })

  const filteredScripts = useMemo(() => {
    const q = scriptSearch.trim().toLowerCase()
    const list = detail?.scripts || []
    if (!q) return list
    return list.filter((s) => s.filename.toLowerCase().includes(q) || s.file_kind.toLowerCase().includes(q))
  }, [detail, scriptSearch])

  const filteredChunks = useMemo(() => {
    const list = selectedScript?.chunks || []
    const q = chunkSearch.trim().toLowerCase()
    if (!q) return list
    return list.filter(
      (c) =>
        c.statement_kind.toLowerCase().includes(q) ||
        c.control_context.toLowerCase().includes(q) ||
        (c.raw_sql || '').toLowerCase().includes(q),
    )
  }, [selectedScript, chunkSearch])

  const filteredEdges = useMemo(() => {
    const list = lineage?.edges || []
    const q = edgeSearch.trim().toLowerCase()
    return list.filter((e) => {
      if (edgeKindFilter !== 'ALL' && e.edge_kind !== edgeKindFilter) return false
      if (!q) return true
      return (
        e.upstream_filename.toLowerCase().includes(q) ||
        e.downstream_filename.toLowerCase().includes(q) ||
        e.via_table.toLowerCase().includes(q)
      )
    })
  }, [lineage, edgeKindFilter, edgeSearch])

  const pagedEdges = useMemo(() => paginateItems(filteredEdges, edgePage, 25), [filteredEdges, edgePage])

  const filteredSchemas = useMemo(() => {
    const q = schemaSearch.trim().toLowerCase()
    if (!q) return schemas
    return schemas.filter(
      (sc) => sc.table_name.toLowerCase().includes(q) || (sc.schema_name || '').toLowerCase().includes(q),
    )
  }, [schemas, schemaSearch])

  if (!selectedProjectId && !busy) {
    return (
      <div>
        <PageHeader
          title="项目导入与血缘台"
          subtitle="统一导入整个 SQL / PL-SQL 项目，自动切块解析、提取 DDL 表字典并建立跨脚本血缘 DAG"
        />
        {error && <div className="alert alert-error">{error}</div>}
        <EmptyState
          title="欢迎使用 DS Agent SQL 工程治理工作台"
          description="您可以一键导入内置 25 个真实 SAC Oracle ETL 跑批脚本进行体验，或导入本地目录 / ZIP 仓库开展全量代码剖析、全局优化与上线前门禁评估。"
          primaryLabel="一键导入真实 SAC 样本库"
          onPrimary={handleQuickSac}
          secondaryLabel="导入本地目录或 ZIP 包"
          onSecondary={() => setImportModalOpen(true)}
        />
      </div>
    )
  }

  const summary = lineage?.summary || {}
  const conflictCount = (summary.write_conflict_tables?.length || 0) + (summary.truncate_conflicts?.length || 0)

  return (
    <div>
      <PageHeader
        title={detail ? detail.name : '项目导入与血缘台'}
        subtitle="按语句级精准切块、识别 Oracle DDL 表结构并追踪跨脚本数据流与写冲突"
        actions={
          detail && (
            <>
              <button type="button" className="btn" disabled={busy} onClick={handleRebuildLineage}>
                重算跨脚本血缘
              </button>
              <button type="button" className="btn btn-primary" onClick={() => navigate('/analysis')}>
                进入优化体检 →
              </button>
              <button type="button" className="btn" disabled={busy} onClick={handleDeleteProject}>
                删除项目
              </button>
            </>
          )
        }
      />

      {error && <div className="alert alert-error">{error}</div>}

      {detail && (
        <>
          <div className="grid-4">
            <div className="kpi-card">
              <span className="kpi-label">纳管脚本 / DDL 文件</span>
              <span className="kpi-value">{detail.script_count}</span>
              <span className="kpi-sub">方言：{detail.dialect}</span>
            </div>
            <div className="kpi-card">
              <span className="kpi-label">解析可执行切块 (Chunks)</span>
              <span className="kpi-value">{detail.chunk_count}</span>
              <span className="kpi-sub">已记录行号与控制流上下文</span>
            </div>
            <div className="kpi-card">
              <span className="kpi-label">识别 DDL 数据表</span>
              <span className="kpi-value">{schemas.length}</span>
              <span className="kpi-sub">含字段类型与主键索引</span>
            </div>
            <div className="kpi-card">
              <span className="kpi-label">跨脚本依赖边 / 冲突数</span>
              <span className="kpi-value">
                {lineage?.edges?.length ?? 0} <span style={{ fontSize: 16, color: conflictCount ? '#b91c1c' : '#6b7280' }}>/ {conflictCount}</span>
              </span>
              <span className="kpi-sub">循环依赖环：{summary.cycles?.length || 0} 组</span>
            </div>
          </div>

          <div className="tabs-bar">
            <button type="button" className={`tab-btn ${tab === 'scripts' ? 'active' : ''}`} onClick={() => setTab('scripts')}>
              脚本与切块剖面 ({detail.script_count})
            </button>
            <button type="button" className={`tab-btn ${tab === 'lineage' ? 'active' : ''}`} onClick={() => setTab('lineage')}>
              跨脚本血缘与冲突 ({lineage?.edges?.length || 0})
            </button>
            <button type="button" className={`tab-btn ${tab === 'schemas' ? 'active' : ''}`} onClick={() => setTab('schemas')}>
              DDL 表结构字典 ({schemas.length})
            </button>
          </div>

          {tab === 'scripts' && (
            <div className="split-pane">
              <div className="master-list">
                <div className="master-list-head">
                  <strong style={{ fontSize: 13 }}>项目脚本列表 ({filteredScripts.length})</strong>
                  <input
                    className="input"
                    placeholder="搜索文件名或类型..."
                    value={scriptSearch}
                    onChange={(e) => setScriptSearch(e.target.value)}
                  />
                </div>
                <div className="master-list-scroll">
                  {filteredScripts.map((s) => (
                    <button
                      type="button"
                      key={s.id}
                      className={`master-item ${selectedScript?.id === s.id ? 'active' : ''}`}
                      onClick={() => handleSelectScript(s.id)}
                    >
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 6 }}>
                        <strong style={{ fontSize: 13, wordBreak: 'break-all' }}>{s.filename}</strong>
                        <span className="badge badge-neutral" style={{ fontSize: 11 }}>{s.file_kind}</span>
                      </div>
                      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11.5, color: '#6b7280' }}>
                        <span>{s.line_count} 行 · {s.chunk_count} 切块 · v{s.current_version}</span>
                        {s.finding_count > 0 && <span style={{ color: '#b45309', fontWeight: 600 }}>{s.finding_count} 问题</span>}
                      </div>
                    </button>
                  ))}
                </div>
              </div>

              <div className="detail-pane">
                {selectedScript ? (
                  <>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 12, flexWrap: 'wrap', marginBottom: 14 }}>
                      <div>
                        <h2 style={{ margin: 0, fontSize: 17 }}>{selectedScript.filename}</h2>
                        <div style={{ fontSize: 12.5, color: '#6b7280', marginTop: 4 }}>
                          {selectedScript.relative_path} · {selectedScript.line_count} 行 · {selectedScript.chunks?.length || 0} 个切块 · COMMIT {selectedScript.commit_count} 次 · 异常捕获 {selectedScript.has_exception_handler ? '已配置' : '缺失'}
                        </div>
                      </div>
                      <div className="tabs-bar" style={{ marginBottom: 0 }}>
                        <button type="button" className={`tab-btn ${chunkMode === 'chunks' ? 'active' : ''}`} onClick={() => setChunkMode('chunks')}>
                          按语句切块浏览
                        </button>
                        <button type="button" className={`tab-btn ${chunkMode === 'full' ? 'active' : ''}`} onClick={() => setChunkMode('full')}>
                          查看完整源码
                        </button>
                      </div>
                    </div>

                    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 14, fontSize: 12 }}>
                      <span className="badge badge-info">写入表 ({(selectedScript.target_tables || []).length})</span>
                      {(selectedScript.target_tables || []).slice(0, 8).map((t) => (
                        <code key={t} style={{ background: '#f3f4f6', padding: '2px 7px', borderRadius: 5 }}>{t}</code>
                      ))}
                      {(selectedScript.target_tables || []).length === 0 && <span style={{ color: '#6b7280' }}>无直接写入表</span>}
                    </div>

                    {chunkMode === 'full' ? (
                      <CodeBlock code={selectedScript.content || ''} title={selectedScript.filename} collapseAfterLines={0} maxHeight={620} />
                    ) : (
                      <>
                        <div style={{ marginBottom: 12 }}>
                          <input
                            className="input"
                            style={{ maxWidth: 340 }}
                            placeholder="过滤切块语句类型、控制上下文或 SQL 关键词..."
                            value={chunkSearch}
                            onChange={(e) => setChunkSearch(e.target.value)}
                          />
                        </div>
                        {filteredChunks.length === 0 ? (
                          <div style={{ color: '#6b7280', padding: 16 }}>没有匹配的切块（或当前 DDL/声明文件未含可执行 DML 切块）。</div>
                        ) : (
                          filteredChunks.map((c) => (
                            <div key={c.id} style={{ marginBottom: 14 }}>
                              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6, flexWrap: 'wrap', gap: 8 }}>
                                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                                  <span className="badge badge-neutral">#{c.chunk_index + 1}</span>
                                  <strong>{c.statement_kind}</strong>
                                  {c.is_dynamic_sql && <span className="badge badge-warn">动态 SQL 已解包</span>}
                                  <span style={{ fontSize: 12, color: '#6b7280' }}>上下文：{c.control_context}</span>
                                </div>
                                <span style={{ fontSize: 12, color: '#4b5563' }}>行 L{c.start_line}–L{c.end_line}</span>
                              </div>
                              <CodeBlock code={c.raw_sql} title={`Chunk #${c.chunk_index + 1}`} startLine={c.start_line} collapseAfterLines={14} />
                            </div>
                          ))
                        )}
                      </>
                    )}
                  </>
                ) : (
                  <div style={{ color: '#6b7280' }}>请在左侧选择一个脚本查看详细切块。</div>
                )}
              </div>
            </div>
          )}

          {tab === 'lineage' && (
            <div>
              <div className="grid-3" style={{ marginBottom: 16 }}>
                <div className="card" style={{ marginBottom: 0 }}>
                  <div style={{ fontSize: 12.5, color: '#6b7280' }}>多脚本并发写表冲突</div>
                  <div style={{ fontSize: 22, fontWeight: 700, margin: '4px 0' }}>{summary.write_conflict_tables?.length || 0} 组</div>
                  <div style={{ fontSize: 12, color: '#4b5563' }}>
                    {(summary.write_conflict_tables || []).slice(0, 3).map((w) => w.table).join(', ') || '未检测到并发写主表冲突'}
                  </div>
                </div>
                <div className="card" style={{ marginBottom: 0 }}>
                  <div style={{ fontSize: 12.5, color: '#6b7280' }}>TRUNCATE 并发清表冲突</div>
                  <div style={{ fontSize: 22, fontWeight: 700, margin: '4px 0' }}>{summary.truncate_conflicts?.length || 0} 组</div>
                  <div style={{ fontSize: 12, color: '#4b5563' }}>
                    {(summary.truncate_conflicts || []).slice(0, 3).map((t) => t.table).join(', ') || '无清表读/写冲突'}
                  </div>
                </div>
                <div className="card" style={{ marginBottom: 0 }}>
                  <div style={{ fontSize: 12.5, color: '#6b7280' }}>血缘环形依赖 (Cycles)</div>
                  <div style={{ fontSize: 22, fontWeight: 700, margin: '4px 0' }}>{summary.cycles?.length || 0} 组</div>
                  <div style={{ fontSize: 12, color: '#4b5563' }}>
                    最长串行链：{summary.longest_chain_hops || 0} 跳
                  </div>
                </div>
              </div>

              <div className="card">
                <div className="filter-bar">
                  <div className="filter-chips">
                    {['ALL', 'DATA_FLOW', 'WRITE_CONFLICT', 'TRUNCATE_CONFLICT'].map((k) => (
                      <button
                        key={k}
                        type="button"
                        className={`filter-chip ${edgeKindFilter === k ? 'active' : ''}`}
                        onClick={() => { setEdgeKindFilter(k); setEdgePage(1) }}
                      >
                        {k === 'ALL' ? '全部血缘边' : k}
                      </button>
                    ))}
                  </div>
                  <input
                    className="input"
                    style={{ maxWidth: 280, marginLeft: 'auto' }}
                    placeholder="搜索脚本名或关联表名..."
                    value={edgeSearch}
                    onChange={(e) => { setEdgeSearch(e.target.value); setEdgePage(1) }}
                  />
                </div>

                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>上游脚本</th>
                        <th>关联数据表</th>
                        <th>边类型</th>
                        <th>下游受影响脚本</th>
                      </tr>
                    </thead>
                    <tbody>
                      {pagedEdges.items.map((e) => (
                        <tr key={e.id}>
                          <td style={{ fontWeight: 500 }}>{e.upstream_filename}</td>
                          <td><code>{e.via_table}</code></td>
                          <td>
                            <span className={`badge ${e.edge_kind === 'DATA_FLOW' ? 'badge-info' : 'badge-p0'}`}>
                              {e.edge_kind}
                            </span>
                          </td>
                          <td style={{ fontWeight: 500 }}>{e.downstream_filename}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>

                <div className="pager">
                  <span>第 {pagedEdges.page} / {pagedEdges.totalPages} 页 · 共 {pagedEdges.total} 条边</span>
                  <div style={{ display: 'flex', gap: 6 }}>
                    <button type="button" className="btn btn-sm" disabled={pagedEdges.page <= 1} onClick={() => setEdgePage((p) => p - 1)}>上一页</button>
                    <button type="button" className="btn btn-sm" disabled={pagedEdges.page >= pagedEdges.totalPages} onClick={() => setEdgePage((p) => p + 1)}>下一页</button>
                  </div>
                </div>
              </div>
            </div>
          )}

          {tab === 'schemas' && (
            <div className="card">
              <div className="filter-bar">
                <input
                  className="input"
                  style={{ maxWidth: 320 }}
                  placeholder="搜索表名或 Schema..."
                  value={schemaSearch}
                  onChange={(e) => setSchemaSearch(e.target.value)}
                />
                <span style={{ fontSize: 12.5, color: '#6b7280' }}>点击任意表行展开字段定义（共 {filteredSchemas.length} 张表）</span>
              </div>
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>数据表名</th>
                      <th>Schema</th>
                      <th>字段数</th>
                      <th>主键字段</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredSchemas.map((sc) => {
                      const isOpen = expandedSchemaId === sc.id
                      return (
                        <React.Fragment key={sc.id}>
                          <tr
                            onClick={() => setExpandedSchemaId(isOpen ? '' : sc.id)}
                            style={{ cursor: 'pointer', background: isOpen ? '#eff6ff' : undefined }}
                          >
                            <td style={{ fontWeight: 600 }}>{sc.table_name}</td>
                            <td>{sc.schema_name || '—'}</td>
                            <td>{(sc.columns || []).length}</td>
                            <td>{(sc.primary_keys || []).join(', ') || '无显式主键'}</td>
                          </tr>
                          {isOpen && (
                            <tr>
                              <td colSpan={4} style={{ background: '#f9fafb', padding: 14 }}>
                                <table className="table">
                                  <thead>
                                    <tr><th>字段名</th><th>数据类型</th><th>可空</th></tr>
                                  </thead>
                                  <tbody>
                                    {(sc.columns || []).map((col, i) => (
                                      <tr key={i}>
                                        <td><code>{col.name}</code></td>
                                        <td>{col.data_type}</td>
                                        <td>{col.nullable ? 'YES' : 'NOT NULL'}</td>
                                      </tr>
                                    ))}
                                  </tbody>
                                </table>
                              </td>
                            </tr>
                          )}
                        </React.Fragment>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}
