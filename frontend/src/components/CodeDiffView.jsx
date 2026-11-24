import React from 'react'

export function CodeDiffView({ diffText, maxLines = 180 }) {
  if (!diffText) {
    return <div style={{ color: '#64748b', fontSize: 13 }}>暂无结构化文本差异（新增脚本或内容未变化）。</div>
  }
  const lines = diffText.split('\n').slice(0, maxLines)
  return (
    <pre className="code-block">
      {lines.map((line, idx) => {
        let cls = ''
        if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('@@')) cls = 'diff-line-hdr'
        else if (line.startsWith('+')) cls = 'diff-line-add'
        else if (line.startsWith('-')) cls = 'diff-line-del'
        return (
          <div key={idx} className={cls}>
            {line}
          </div>
        )
      })}
    </pre>
  )
}
