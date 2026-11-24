import React, { useState } from 'react'

export function CodeBlock({
  code = '',
  title = 'SQL',
  startLine = 1,
  collapseAfterLines = 18,
  maxHeight,
}) {
  const [copied, setCopied] = useState(false)
  const [expanded, setExpanded] = useState(false)

  const lines = String(code || '').replace(/\r\n/g, '\n').split('\n')
  const needsCollapse = collapseAfterLines > 0 && lines.length > collapseAfterLines
  const shownLines = needsCollapse && !expanded ? lines.slice(0, collapseAfterLines) : lines

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(String(code || ''))
      setCopied(true)
      setTimeout(() => setCopied(false), 1800)
    } catch {
      /* ignore clipboard fallback */
    }
  }

  return (
    <div className="code-shell">
      <div className="code-shell-head">
        <span>
          <strong>{title}</strong>
          <span style={{ marginLeft: 8, color: '#6b7280' }}>共 {lines.length} 行</span>
        </span>
        <div style={{ display: 'flex', gap: 6 }}>
          {needsCollapse && (
            <button
              type="button"
              className="btn btn-sm"
              onClick={() => setExpanded((prev) => !prev)}
            >
              {expanded ? '收起代码' : `展开全部 ${lines.length} 行`}
            </button>
          )}
          <button type="button" className="btn btn-sm" onClick={handleCopy}>
            {copied ? '已复制' : '复制'}
          </button>
        </div>
      </div>
      <pre className="code-pre" style={maxHeight ? { maxHeight, overflowY: 'auto' } : undefined}>
        {shownLines.map((line, idx) => (
          <div key={idx} className="code-line">
            <span className="code-lnum">{startLine + idx}</span>
            <span className="code-ltxt">{line}</span>
          </div>
        ))}
      </pre>
    </div>
  )
}
