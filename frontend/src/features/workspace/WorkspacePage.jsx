import React, { useEffect, useState } from 'react'
import { PageHeader } from '../../components/PageHeader.jsx'
import { useProjectContext } from '../../context/ProjectContext.jsx'
import { useAsync } from '../../hooks/useAsync.js'
import {
  deleteProject,
  getProjectDetail,
  getProjectLineage,
  getScriptDetail,
  importDirProject,
  importSacProject,
  importZipProject,
  listProjectSchemas,
  rebuildProjectLineage,
} from '../../api/workspace.js'

export default function WorkspacePage() {
  const { selectedProjectId, refreshProjects } = useProjectContext()
  const { busy, error, setError, run } = useAsync()

  const [detail, setDetail] = useState(null)
  const [lineage, setLineage] = useState(null)
  const [schemas, setSchemas] = useState([])
  const [selectedScript, setSelectedScript] = useState(null)

  const [sacName, setSacName] = useState('SAC 真实 Oracle ETL 资产库')
  const [dirName, setDirName] = useState('本地 SQL 项目')
  const [dirPath, setDirPath] = useState('')
  const [zipFile, setZipFile] = useState(null)
  const [zipName, setZipName] = useState('ZIP 上传 SQL 仓库')
  const [notice, setNotice] = useState('')

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
    loadWorkspaceData(selectedProjectId).catch((e) => setError(e?.message || '加载失败'))
  }, [selectedProjectId])

  const handleImportSac = () =>
    run(async () => {
      const created = await importSacProject(sacName.trim())
      setNotice(`已导入 SAC 资产库：${created.script_count} 个脚本，${created.chunk_count} 个可执行切块，${created.table_count} 张 DDL 表。`)
      await refreshProjects(created.id)
    })

  const handleImportDir = (e) => {
    e.preventDefault()
    if (!dirPath.trim()) return setError('请输入本地 SQL 仓库绝对路径')
    run(async () => {
      const created = await importDirProject({ name: dirName.trim() || '本地目录项目', dir_path: dirPath.trim() })
      setNotice(`目录导入完成：${created.script_count} 个脚本，${created.chunk_count} 个切块。`)
      await refreshProjects(created.id)
    })
  }

  const handleImportZip = (e) => {
    e.preventDefault()
    if (!zipFile) return setError('请选择 .zip SQL 项目包')
    run(async () => {
      const created = await importZipProject(zipFile, zipName.trim() || 'ZIP 项目')
      setNotice(`ZIP 解压导入完成：${created.script_count} 个脚本，${created.chunk_count} 个切块。`)
      await refreshProjects(created.id)
    })
  }

  const handleSelectScript = (scriptId) =>
    run(async () => {
      const sd = await getScriptDetail(selectedProjectId, scriptId)
      setSelectedScript(sd)
    })

  const handleRebuildLineage = () =>
    run(async () => {
      const lin = await rebuildProjectLineage(selectedProjectId)
      setLineage(lin)
      setNotice(`已重算项目血缘：${lin.edges?.length || 0} 条表级边。`)
    })

  const handleDeleteProject = () =>
    run(async () => {
      if (!selectedProjectId) return
      await deleteProject(selectedProjectId)
      setNotice('已删除选中项目。')
      await refreshProjects('')
    })

  const summary = lineage?.summary || {}

  return (
    <div>
      <PageHeader
        title="工作台与全项目导入"
        subtitle="支持整仓导入 SQL/PL-SQL 项目、自动识别 DDL Schema、切分可执行语句块并构建表级跨脚本血缘 DAG"
        actions={
          selectedProjectId && (
            <>
              <button type="button" className="btn" disabled={busy} onClick={handleRebuildLineage}>
                重算跨脚本血缘
              </button>
              <button type="button" className="btn btn-danger" disabled={busy} onClick={handleDeleteProject}>
                删除当前项目
              </button>
            </>
          )
        }
      />

      {error && <div className="alert alert-error">{error}</div>}
      {notice && <div className="alert alert-info">{notice}</div>}

      <div className="grid-2">
        <div className="card">
          <h3 style={{ marginTop: 0 }}>快速导入真实 SAC 项目（25 脚本 / 1.6 万行）</h3>
          <div style={{ display: 'flex', gap: 8 }}>
            <input
              className="input"
              value={sacName}
              onChange={(e) => setSacName(e.target.value)}
              placeholder="项目名称"
            />
            <button type="button" className="btn btn-primary" disabled={busy} onClick={handleImportSac}>
              {busy ? '导入解析中...' : '一键导入 SAC 样本库'}
            </button>
          </div>
          <div style={{ fontSize: 12, color: '#64748b', marginTop: 8 }}>
            自动解析 Oracle PL/SQL 存储过程、`EXECUTE IMMEDIATE` 动态语句与 `SAC_TABLE_SCHEMAS.sql` DDL。
          </div>
        </div>

        <div className="card">
          <h3 style={{ marginTop: 0 }}>导入本地目录或 ZIP 压缩包</h3>
          <form onSubmit={handleImportDir} style={{ display: 'flex', gap: 8, marginBottom: 10 }}>
            <input className="input" style={{ width: 150 }} value={dirName} onChange={(e) => setDirName(e.target.value)} placeholder="项目名" />
            <input className="input" value={dirPath} onChange={(e) => setDirPath(e.target.value)} placeholder="/path/to/sql_repo" />
            <button type="submit" className="btn" disabled={busy}>导入目录</button>
          </form>
          <form onSubmit={handleImportZip} style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <input className="input" style={{ width: 150 }} value={zipName} onChange={(e) => setZipName(e.target.value)} placeholder="ZIP 项目名" />
            <input type="file" accept=".zip" onChange={(e) => setZipFile(e.target.files?.[0] || null)} />
            <button type="submit" className="btn" disabled={busy}>上传 ZIP</button>
          </form>
        </div>
      </div>

      {detail && (
        <>
          <div className="grid-4" style={{ marginBottom: 16 }}>
            <div className="kpi-card">
              <div className="kpi-label">纳管脚本 / DDL</div>
              <div className="kpi-value">{detail.script_count}</div>
            </div>
            <div className="kpi-card">
              <div className="kpi-label">解析语句切块（Chunks）</div>
              <div className="kpi-value">{detail.chunk_count}</div>
            </div>
            <div className="kpi-card">
              <div className="kpi-label">识别 DDL 数据表</div>
              <div className="kpi-value">{schemas.length}</div>
            </div>
            <div className="kpi-card">
              <div className="kpi-label">血缘边 / 写冲突</div>
              <div className="kpi-value">
                {lineage?.edges?.length ?? 0} / {(summary.write_conflict_tables?.length || 0) + (summary.truncate_conflicts?.length || 0)}
              </div>
            </div>
          </div>

          <div className="grid-2">
            <div className="card" style={{ maxHeight: 520, overflowY: 'auto' }}>
              <h3 style={{ marginTop: 0 }}>脚本清单（点击查看切块）</h3>
              <table className="table">
                <thead>
                  <tr>
                    <th>文件名</th>
                    <th>类型</th>
                    <th>行数 / 切块</th>
                    <th>目标表数</th>
                  </tr>
                </thead>
                <tbody>
                  {(detail.scripts || []).map((s) => (
                    <tr
                      key={s.id}
                      onClick={() => handleSelectScript(s.id)}
                      style={{
                        cursor: 'pointer',
                        background: selectedScript?.id === s.id ? '#eff6ff' : 'transparent',
                      }}
                    >
                      <td>
                        <div style={{ fontWeight: 600 }}>{s.filename}</div>
                        <div style={{ fontSize: 11, color: '#64748b' }}>v{s.current_version} · {s.dialect}</div>
                      </td>
                      <td>{s.file_kind}</td>
                      <td>{s.line_count} / {s.chunk_count}</td>
                      <td>{(s.target_tables || []).length}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="card" style={{ maxHeight: 520, overflowY: 'auto' }}>
              <h3 style={{ marginTop: 0 }}>
                {selectedScript ? `切块剖面：${selectedScript.filename}` : '选择一个脚本查看切块'}
              </h3>
              {selectedScript && (
                <div>
                  <div style={{ fontSize: 12, color: '#475569', marginBottom: 10 }}>
                    写入表：{(selectedScript.target_tables || []).join(', ') || '无'} | 来源表：{(selectedScript.source_tables || []).slice(0, 8).join(', ') || '无'}
                  </div>
                  {(selectedScript.chunks || []).map((c) => (
                    <div key={c.id} style={{ border: '1px solid #e2e8f0', borderRadius: 8, padding: 10, marginBottom: 10 }}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, marginBottom: 6 }}>
                        <strong>#{c.chunk_index + 1} {c.statement_kind} {c.is_dynamic_sql ? '(动态SQL)' : ''}</strong>
                        <span style={{ color: '#64748b' }}>L{c.start_line}-L{c.end_line} · {c.control_context}</span>
                      </div>
                      <pre className="code-block" style={{ maxHeight: 150, overflowY: 'auto' }}>{c.raw_sql}</pre>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>

          <div className="card">
            <h3 style={{ marginTop: 0 }}>跨脚本血缘与结构风险摘要（Top 40 依赖边）</h3>
            <div style={{ fontSize: 13, color: '#475569', marginBottom: 10 }}>
              最长串行链（{summary.longest_chain_hops || 0} 跳）：{(summary.longest_chain || []).join(' → ') || '无'} · 环路数量：{summary.cycles?.length || 0}
            </div>
            <table className="table">
              <thead>
                <tr>
                  <th>上游脚本</th>
                  <th>关联数据表</th>
                  <th>依赖类型</th>
                  <th>下游脚本</th>
                </tr>
              </thead>
              <tbody>
                {(lineage?.edges || []).slice(0, 40).map((e) => (
                  <tr key={e.id}>
                    <td>{e.upstream_filename}</td>
                    <td><code>{e.via_table}</code></td>
                    <td>{e.edge_kind}</td>
                    <td>{e.downstream_filename}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  )
}
