import React, { useState } from 'react'

export function CodeDiffView({ diffText, maxLines = 300 }) {
  const [copied, setCopied] = useState(false)

  if (!diffText) {
    return (
      <div style={{ padding: 20, textAlign: 'center', color: '#6b7280', background: '#f9fafb', borderRadius: 8, border: '1px solid #e5e7eb' }}>
        暂无文本 Diff（新增脚本或源码未变动）。
      </div>
    )
  }

  const lines = diffText.split('\n')
  const shown = lines.slice(0, maxLines)

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(diffText)
      setCopied(true)
      setTimeout(() => setCopied(false), 1800)
    } catch {
      /* ignore */
    }
  }

  return (
    <div className="code-shell">
      <div className="code-shell-head">
        <span><strong>Unified Diff</strong>（展示 {shown.length} / {lines.length} 行）</span>
        <button type="button" className="btn btn-sm" onClick={handleCopy}>
          {copied ? '已复制' : '复制 Diff'}
        </button>
      </div>
      <pre className="code-pre" style={{ maxHeight: 520, overflowY: 'auto' }}>
        {shown.map((line, idx) => {
          let cls = ''
          if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('@@')) cls = 'diff-line-hdr'
          else if (line.startsWith('+')) cls = 'diff-line-add'
          else if (line.startsWith('-')) cls = 'diff-line-del'
          return (
            <div key={idx} className={`code-line ${cls}`}>
              <span className="code-lnum">{idx + 1}</span>
              <span className="code-ltxt">{line}</span>
            </div>
          )
        })}
      </pre>
    </div>
  )
}
