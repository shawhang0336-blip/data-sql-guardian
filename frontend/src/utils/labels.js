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

export const ROADMAP_PRIORITY_META = {
  P0_BLOCKER: {
    key: 'P0_BLOCKER',
    rank: 0,
    phaseCode: '阶段 0',
    phaseTitle: '止血阻断 · 立刻处置',
    phaseDesc: '1–2 周 · 破环/消清表并发/日志收口',
    label: 'P0 阻断',
    badgeClass: 'badge-p0',
    accentColor: '#b91c1c',
  },
  P1_HIGH: {
    key: 'P1_HIGH',
    rank: 1,
    phaseCode: '阶段 1',
    phaseTitle: '分层与公共层抽离',
    phaseDesc: '3–6 周 · L0–L3 分层与公共维度',
    label: 'P1 高优',
    badgeClass: 'badge-p1',
    accentColor: '#b45309',
  },
  P2_MEDIUM: {
    key: 'P2_MEDIUM',
    rank: 2,
    phaseCode: '阶段 2',
    phaseTitle: '性能调优与口径治理',
    phaseDesc: '4–8 周 · 枢纽索引/合并宽链路',
    label: 'P2 中优',
    badgeClass: 'badge-info',
    accentColor: '#2563eb',
  },
  P3_LOW: {
    key: 'P3_LOW',
    rank: 3,
    phaseCode: '阶段 3',
    phaseTitle: '长效规范与 CI 卡点',
    phaseDesc: '持续演进 · 血缘注册与规范检查',
    label: 'P3 建议',
    badgeClass: 'badge-neutral',
    accentColor: '#4b5563',
  },
}

export function normalizeRoadmapPriority(priority) {
  const raw = String(priority || 'P2_MEDIUM').toUpperCase()
  if (ROADMAP_PRIORITY_META[raw]) return raw
  if (raw.startsWith('P0')) return 'P0_BLOCKER'
  if (raw.startsWith('P1')) return 'P1_HIGH'
  if (raw.startsWith('P3')) return 'P3_LOW'
  return 'P2_MEDIUM'
}

export const ROADMAP_CATEGORY_META = {
  ARCHITECTURE: '架构解耦',
  TRANSACTION: '事务安全',
  IDEMPOTENCY: '重跑幂等',
  PERFORMANCE: '性能调优',
  CORRECTNESS: '正确性修复',
  MAINTAINABILITY: '可维护性',
  RELIABILITY: '稳定性',
  SECURITY: '安全合规',
  GOVERNANCE: '资产治理',
}

export function parseExecutiveSummary(text) {
  const raw = String(text || '').trim()
  if (!raw) return { lead: '', sections: [], footer: '' }

  const paragraphs = raw
    .split(/\n{2,}/)
    .map((p) => p.trim())
    .filter(Boolean)

  if (paragraphs.length === 1) {
    return { lead: paragraphs[0], sections: [], footer: '' }
  }

  const lead = paragraphs[0]
  const sections = []
  let footer = ''

  for (let i = 1; i < paragraphs.length; i += 1) {
    const p = paragraphs[i]
    const headingMatch = p.match(/^([一二三四五六七八九十]+、[^。：\n]+|治理路线[^。：\n]*)[。：:]\s*([\s\S]*)$/)
    if (headingMatch) {
      const heading = headingMatch[1].trim()
      const body = headingMatch[2].trim()
      if (heading.startsWith('治理路线')) {
        footer = `${heading}：${body}`
      } else {
        sections.push({ heading, body })
      }
    } else if (i === paragraphs.length - 1 && (p.includes('阶段 0') || p.includes('所有改造'))) {
      footer = p
    } else {
      const splitIdx = p.indexOf('。')
      if (splitIdx > 6 && splitIdx < 40) {
        sections.push({ heading: p.slice(0, splitIdx), body: p.slice(splitIdx + 1).trim() })
      } else {
        sections.push({ heading: `评估要点 ${sections.length + 1}`, body: p })
      }
    }
  }

  return { lead, sections, footer }
}

export function parseProposalSteps(proposal) {
  const raw = String(proposal || '').trim()
  if (!raw) return { intro: '', steps: [] }

  // Match `(1)` / `（1）` / `1.` / `1、` after start, whitespace, or Chinese/ASCII punctuation
  const markerRe = /(?:^|[\s：:。；;])([\(（]\d+[\)）]|\d+[\.、])\s*/g
  const matches = [...raw.matchAll(markerRe)]
  if (matches.length < 2) {
    return { intro: raw, steps: [] }
  }

  const firstIdx = matches[0].index
  const intro = raw.slice(0, firstIdx).trim().replace(/[：:]$/, '')
  const steps = []
  for (let i = 0; i < matches.length; i += 1) {
    const start = matches[i].index + matches[i][0].length
    const end = i + 1 < matches.length ? matches[i + 1].index : raw.length
    const piece = raw.slice(start, end).trim()
    if (piece) steps.push(piece)
  }
  return { intro, steps }
}

export function formatRoadmapMarkdown(roadmap, projectName = 'SQL 工程项目') {
  if (!roadmap) return ''
  const lines = [
    `# ${projectName} — AI 全项目架构重构路线图`,
    '',
    '## 总体评估总结',
    '',
    roadmap.executive_summary || '暂无总结',
    '',
    '## 分阶段架构治理建议',
    '',
  ]

  for (const rec of roadmap.architecture_recommendations || []) {
    const pKey = normalizeRoadmapPriority(rec.priority)
    const pMeta = ROADMAP_PRIORITY_META[pKey]
    const catLabel = ROADMAP_CATEGORY_META[rec.category] || rec.category
    lines.push(`### [${pMeta.label} · ${catLabel}] ${rec.title}`)
    lines.push(`- **阶段归属**：${pMeta.phaseCode}（${pMeta.phaseTitle}）`)
    if ((rec.affected_tables || []).length) {
      lines.push(`- **涉及数据表**：${rec.affected_tables.map((t) => `\`${t}\``).join('、')}`)
    }
    if ((rec.affected_scripts || []).length) {
      lines.push(`- **涉及脚本**：${rec.affected_scripts.map((s) => `\`${s}\``).join('、')}`)
    }
    lines.push(`- **改造方案**：${rec.proposal}`)
    lines.push(`- **预期收益**：${rec.expected_benefit}`)
    lines.push('')
  }

  return lines.join('\n')
}

export function formatUtcTime(iso) {
  if (!iso) return '—'
  const clean = iso.endsWith('Z') || /[+-]\d{2}:\d{2}$/.test(iso) ? iso : `${iso}Z`
  const d = new Date(clean)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })
}

export function paginateItems(items, page = 1, pageSize = 20) {
  const list = Array.isArray(items) ? items : []
  const size = Math.max(1, Number(pageSize) || 20)
  const totalPages = Math.max(1, Math.ceil(list.length / size))
  const safePage = Math.min(Math.max(1, Number(page) || 1), totalPages)
  const start = (safePage - 1) * size
  return {
    items: list.slice(start, start + size),
    page: safePage,
    pageSize: size,
    total: list.length,
    totalPages,
  }
}

export function groupFindingsBySeverity(findings) {
  const counts = { P0: 0, P1: 0, P2: 0, resolved: 0, total: 0 }
  for (const f of findings || []) {
    counts.total += 1
    if (f?.severity === 'P0') counts.P0 += 1
    else if (f?.severity === 'P1') counts.P1 += 1
    else counts.P2 += 1
    if (f?.status && f.status !== 'OPEN') counts.resolved += 1
  }
  return counts
}
