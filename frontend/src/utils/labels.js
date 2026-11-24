export const SEVERITY_META = {
  P0: { label: 'P0 阻断', badgeClass: 'badge-p0' },
  P1: { label: 'P1 高优', badgeClass: 'badge-p1' },
  P2: { label: 'P2 建议', badgeClass: 'badge-p2' },
}

export const VERDICT_META = {
  BLOCK: { label: 'BLOCK 阻断上线', badgeClass: 'badge-block' },
  WARN: { label: 'WARN 带条件告警', badgeClass: 'badge-warn' },
  PASS: { label: 'PASS 建议通过', badgeClass: 'badge-pass' },
}

export const STATUS_META = {
  OPEN: { label: '待处理', badgeClass: 'badge-neutral' },
  ADOPTED: { label: '已采纳', badgeClass: 'badge-adopted' },
  IGNORED: { label: '已忽略', badgeClass: 'badge-neutral' },
  WAITING_APPROVAL: { label: '待人工审批', badgeClass: 'badge-waiting' },
  APPROVED: { label: '已批准合并', badgeClass: 'badge-approved' },
  REJECTED: { label: '已驳回', badgeClass: 'badge-rejected' },
}

export function formatUtcTime(iso) {
  if (!iso) return '—'
  const clean = iso.endsWith('Z') || /[+-]\d{2}:\d{2}$/.test(iso) ? iso : `${iso}Z`
  const d = new Date(clean)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })
}
