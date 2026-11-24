import React from 'react'
import { SEVERITY_META, STATUS_META, VERDICT_META } from '../utils/labels.js'

export function StatusBadge({ kind, value }) {
  const dict =
    kind === 'severity'
      ? SEVERITY_META
      : kind === 'verdict'
        ? VERDICT_META
        : STATUS_META
  const meta = dict[value] || { label: value || '—', badgeClass: 'badge-neutral' }
  return <span className={`badge ${meta.badgeClass}`}>{meta.label}</span>
}
