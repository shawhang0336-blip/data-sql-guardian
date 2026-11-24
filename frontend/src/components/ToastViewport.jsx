import React from 'react'
import { useProjectContext } from '../context/ProjectContext.jsx'

export function ToastViewport() {
  const { toasts, dismissToast } = useProjectContext()
  if (!toasts.length) return null

  return (
    <div className="toast-viewport" role="status" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} className={`toast-item ${t.type || 'info'}`}>
          <span>{t.message}</span>
          <button
            type="button"
            className="btn btn-sm"
            style={{ padding: '1px 7px' }}
            onClick={() => dismissToast(t.id)}
            aria-label="关闭通知"
          >
            ×
          </button>
        </div>
      ))}
    </div>
  )
}
