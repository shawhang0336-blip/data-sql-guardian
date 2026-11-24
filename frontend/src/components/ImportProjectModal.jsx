import React, { useState } from 'react'
import { useProjectContext } from '../context/ProjectContext.jsx'
import { useAsync } from '../hooks/useAsync.js'
import { importDirProject, importSacProject, importZipProject } from '../api/workspace.js'

export function ImportProjectModal() {
  const { importModalOpen, setImportModalOpen, refreshProjects, pushToast } = useProjectContext()
  const { busy, error, setError, run } = useAsync()

  const [mode, setMode] = useState('sac') // sac | dir | zip
  const [sacName, setSacName] = useState('SAC 真实 Oracle ETL 资产库')
  const [dirName, setDirName] = useState('本地 SQL 项目')
  const [dirPath, setDirPath] = useState('')
  const [zipName, setZipName] = useState('ZIP 上传 SQL 仓库')
  const [zipFile, setZipFile] = useState(null)

  if (!importModalOpen) return null

  const closeModal = () => {
    if (!busy) {
      setError('')
      setImportModalOpen(false)
    }
  }

  const onSuccess = async (created, label) => {
    await refreshProjects(created.id)
    setImportModalOpen(false)
    pushToast('success', `${label}成功：${created.script_count} 个脚本，${created.chunk_count} 个切块，${created.table_count} 张 DDL 表`)
  }

  const handleSac = (e) => {
    e.preventDefault()
    run(async () => {
      const created = await importSacProject(sacName.trim())
      await onSuccess(created, 'SAC 样本库导入')
    })
  }

  const handleDir = (e) => {
    e.preventDefault()
    if (!dirPath.trim()) return setError('请输入本地 SQL 项目根目录路径')
    run(async () => {
      const created = await importDirProject({ name: dirName.trim() || '本地 SQL 项目', dir_path: dirPath.trim() })
      await onSuccess(created, '本地目录导入')
    })
  }

  const handleZip = (e) => {
    e.preventDefault()
    if (!zipFile) return setError('请先选择 .zip 压缩包')
    run(async () => {
      const created = await importZipProject(zipFile, zipName.trim() || 'ZIP SQL 仓库')
      await onSuccess(created, 'ZIP 压缩包导入')
    })
  }

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="导入 SQL 项目">
      <div className="modal-card">
        <div className="modal-head">
          <div>
            <strong style={{ fontSize: 16 }}>导入 SQL / ETL 工程项目</strong>
            <div style={{ fontSize: 12, color: '#6b7280' }}>自动切分语句块、识别 Oracle DDL 并构建跨脚本血缘拓扑</div>
          </div>
          <button type="button" className="btn btn-sm" onClick={closeModal} disabled={busy}>关闭</button>
        </div>

        <div className="modal-body">
          <div className="tabs-bar">
            <button type="button" className={`tab-btn ${mode === 'sac' ? 'active' : ''}`} onClick={() => setMode('sac')}>
              一键导入 SAC 样本库
            </button>
            <button type="button" className={`tab-btn ${mode === 'dir' ? 'active' : ''}`} onClick={() => setMode('dir')}>
              导入本地目录
            </button>
            <button type="button" className={`tab-btn ${mode === 'zip' ? 'active' : ''}`} onClick={() => setMode('zip')}>
              上传 ZIP 包
            </button>
          </div>

          {error && <div className="alert alert-error">{error}</div>}

          {mode === 'sac' && (
            <form onSubmit={handleSac} style={{ display: 'grid', gap: 12 }}>
              <div style={{ fontSize: 13, color: '#4b5563' }}>
                内置导入 `sql_explorer/sample_scripts` 下 25 个真实 Oracle SAC 跑批脚本（1.6 万行代码 + 全量 DDL）。
              </div>
              <label>
                <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 4 }}>项目名称</div>
                <input className="input" value={sacName} onChange={(e) => setSacName(e.target.value)} />
              </label>
              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
                <button type="button" className="btn" onClick={closeModal} disabled={busy}>取消</button>
                <button type="submit" className="btn btn-primary" disabled={busy}>
                  {busy ? '正在解析与构建血缘...' : '立即导入 SAC 样本库'}
                </button>
              </div>
            </form>
          )}

          {mode === 'dir' && (
            <form onSubmit={handleDir} style={{ display: 'grid', gap: 12 }}>
              <label>
                <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 4 }}>项目名称</div>
                <input className="input" value={dirName} onChange={(e) => setDirName(e.target.value)} />
              </label>
              <label>
                <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 4 }}>服务器本地绝对路径</div>
                <input className="input" value={dirPath} onChange={(e) => setDirPath(e.target.value)} placeholder="/path/to/etl_sql_repo" />
              </label>
              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
                <button type="button" className="btn" onClick={closeModal} disabled={busy}>取消</button>
                <button type="submit" className="btn btn-primary" disabled={busy}>
                  {busy ? '正在扫描目录...' : '确认导入目录'}
                </button>
              </div>
            </form>
          )}

          {mode === 'zip' && (
            <form onSubmit={handleZip} style={{ display: 'grid', gap: 12 }}>
              <label>
                <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 4 }}>项目名称</div>
                <input className="input" value={zipName} onChange={(e) => setZipName(e.target.value)} />
              </label>
              <label>
                <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 4 }}>选择 `.zip` 压缩文件</div>
                <input type="file" accept=".zip" onChange={(e) => setZipFile(e.target.files?.[0] || null)} />
              </label>
              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
                <button type="button" className="btn" onClick={closeModal} disabled={busy}>取消</button>
                <button type="submit" className="btn btn-primary" disabled={busy}>
                  {busy ? '正在安全解压...' : '上传并导入'}
                </button>
              </div>
            </form>
          )}
        </div>
      </div>
    </div>
  )
}
