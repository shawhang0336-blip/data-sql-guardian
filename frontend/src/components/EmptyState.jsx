import React from 'react'

export function EmptyState({ title, description, primaryLabel, onPrimary, secondaryLabel, onSecondary }) {
  return (
    <div className="empty-card">
      <svg width="48" height="48" viewBox="0 0 48 48" fill="none" style={{ marginBottom: 12 }} aria-hidden="true">
        <rect x="6" y="8" width="36" height="32" rx="6" fill="#eff6ff" stroke="#93c5fd" strokeWidth="1.8" />
        <path d="M14 19h20M14 25h14M14 31h10" stroke="#2563eb" strokeWidth="2" strokeLinecap="round" />
      </svg>
      <h2 style={{ margin: '0 0 8px', fontSize: 18 }}>{title}</h2>
      <p style={{ margin: '0 auto 20px', maxWidth: 480, color: '#6b7280', fontSize: 13.5 }}>{description}</p>
      <div style={{ display: 'flex', justifyContent: 'center', gap: 10, flexWrap: 'wrap' }}>
        {primaryLabel && (
          <button type="button" className="btn btn-primary" onClick={onPrimary}>
            {primaryLabel}
          </button>
        )}
        {secondaryLabel && (
          <button type="button" className="btn" onClick={onSecondary}>
            {secondaryLabel}
          </button>
        )}
      </div>
    </div>
  )
}
