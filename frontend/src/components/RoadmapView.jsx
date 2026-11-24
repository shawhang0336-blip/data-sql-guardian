import React, { useMemo, useState } from 'react'
import { EmptyState } from './EmptyState.jsx'
import {
  ROADMAP_CATEGORY_META,
  ROADMAP_PRIORITY_META,
  formatRoadmapMarkdown,
  normalizeRoadmapPriority,
  parseExecutiveSummary,
  parseProposalSteps,
} from '../utils/labels.js'

const PHASE_KEYS = ['P0_BLOCKER', 'P1_HIGH', 'P2_MEDIUM', 'P3_LOW']

export function RoadmapView({
  roadmap,
  projectName = '',
  busy = false,
  onRegenerate,
  onSelectScriptName,
}) {
  const [phaseFilter, setPhaseFilter] = useState('')
  const [summaryExpanded, setSummaryExpanded] = useState(true)
  const [expandedCards, setExpandedCards] = useState(() => new Set(['P0_BLOCKER-0', 'P0_BLOCKER-1', 'P0_BLOCKER-2']))
  const [expandedTables, setExpandedTables] = useState(() => new Set())
  const [copiedMd, setCopiedMd] = useState(false)

  const parsedSummary = useMemo(
    () => parseExecutiveSummary(roadmap?.executive_summary || ''),
    [roadmap?.executive_summary],
  )

  const groupedByPhase = useMemo(() => {
    const groups = { P0_BLOCKER: [], P1_HIGH: [], P2_MEDIUM: [], P3_LOW: [] }
    for (const rec of roadmap?.architecture_recommendations || []) {
      const k = normalizeRoadmapPriority(rec.priority)
      groups[k].push(rec)
    }
    return groups
  }, [roadmap?.architecture_recommendations])

  if (!roadmap?.executive_summary) {
    return (
      <div className="card">
        <EmptyState
          title="尚未生成 AI 全项目架构重构路线图"
          description="结合跨脚本血缘、写表冲突、TRUNCATE 并发风险与高频枢纽表，由大模型统一梳理分阶段重构建议与公共层抽取方案。"
          primaryLabel={busy ? '正在合成全仓路线图...' : '立即运行 AI 全仓架构合成'}
          onPrimary={onRegenerate}
        />
      </div>
    )
  }

  const totalCount = (roadmap.architecture_recommendations || []).length

  const toggleCard = (cardKey) => {
    setExpandedCards((prev) => {
      const next = new Set(prev)
      if (next.has(cardKey)) next.delete(cardKey)
      else next.add(cardKey)
      return next
    })
  }

  const expandAll = () => {
    const all = new Set()
    for (const pk of PHASE_KEYS) {
      groupedByPhase[pk].forEach((_, idx) => all.add(`${pk}-${idx}`))
    }
    setExpandedCards(all)
  }

  const collapseAll = () => setExpandedCards(new Set())

  const toggleTables = (e, cardKey) => {
    e.stopPropagation()
    setExpandedTables((prev) => {
      const next = new Set(prev)
      if (next.has(cardKey)) next.delete(cardKey)
      else next.add(cardKey)
      return next
    })
  }

  const handleCopyMarkdown = async () => {
    try {
      await navigator.clipboard.writeText(formatRoadmapMarkdown(roadmap, projectName))
      setCopiedMd(true)
      setTimeout(() => setCopiedMd(false), 2000)
    } catch {
      /* ignore clipboard fallback */
    }
  }

  const visiblePhases = PHASE_KEYS.filter(
    (pk) => groupedByPhase[pk].length > 0 && (!phaseFilter || phaseFilter === pk),
  )

  return (
    <div style={{ display: 'grid', gap: 16 }}>
      {/* 1. Executive Summary Card */}
      <div className="roadmap-summary-card">
        <div className="card-title" style={{ marginBottom: 10 }}>
          <div>
            <span style={{ fontSize: 16, fontWeight: 700 }}>全仓架构体检总结与四阶段治理路线</span>
            <span className="badge badge-info" style={{ marginLeft: 10 }}>共 {totalCount} 项重构建议</span>
          </div>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            {parsedSummary.sections.length > 0 && (
              <button type="button" className="btn btn-sm" onClick={() => setSummaryExpanded((v) => !v)}>
                {summaryExpanded ? '收起诊断分项' : `展开诊断分项 (${parsedSummary.sections.length})`}
              </button>
            )}
            <button type="button" className="btn btn-sm" onClick={handleCopyMarkdown}>
              {copiedMd ? '已复制 Markdown' : '复制路线图 MD'}
            </button>
            <button type="button" className="btn btn-sm btn-primary" disabled={busy} onClick={onRegenerate}>
              {busy ? '重新生成中...' : '重新合成路线图'}
            </button>
          </div>
        </div>

        <p className="roadmap-lead">{parsedSummary.lead}</p>

        {summaryExpanded && parsedSummary.sections.length > 0 && (
          <div className="roadmap-insight-grid">
            {parsedSummary.sections.map((sec, idx) => (
              <div key={idx} className="roadmap-insight-item">
                <div className="roadmap-insight-head">{sec.heading}</div>
                <p className="roadmap-insight-body">{sec.body}</p>
              </div>
            ))}
          </div>
        )}

        {parsedSummary.footer && (
          <div className="roadmap-footer-callout">
            {parsedSummary.footer}
          </div>
        )}
      </div>

      {/* 2. 4-Phase Interactive Stepper */}
      <div className="roadmap-phases">
        {PHASE_KEYS.map((pk) => {
          const meta = ROADMAP_PRIORITY_META[pk]
          const count = groupedByPhase[pk].length
          const isActive = phaseFilter === pk
          return (
            <button
              key={pk}
              type="button"
              className={`roadmap-phase-card ${isActive ? 'active' : ''}`}
              style={{ '--phase-accent': meta.accentColor }}
              onClick={() => setPhaseFilter((curr) => (curr === pk ? '' : pk))}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <span className="roadmap-phase-code">{meta.phaseCode}</span>
                <span className={`badge ${meta.badgeClass}`}>{count} 项</span>
              </div>
              <div className="roadmap-phase-title">{meta.phaseTitle}</div>
              <div className="roadmap-phase-desc">{meta.phaseDesc}</div>
            </button>
          )
        })}
      </div>

      {/* 3. Controls & Grouped Expandable Cards */}
      <div className="card" style={{ marginBottom: 0 }}>
        <div className="filter-bar" style={{ justifyContent: 'space-between', marginBottom: 14 }}>
          <div style={{ fontSize: 13, color: '#4b5563' }}>
            {phaseFilter
              ? `当前仅看：${ROADMAP_PRIORITY_META[phaseFilter].phaseCode} · ${ROADMAP_PRIORITY_META[phaseFilter].phaseTitle}`
              : '按治理优先级分阶段展示（默认展开 P0 关键阻断项，点击标题展开/收起实施步骤）'}
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            {phaseFilter && (
              <button type="button" className="btn btn-sm" onClick={() => setPhaseFilter('')}>显示全部阶段</button>
            )}
            <button type="button" className="btn btn-sm" onClick={expandAll}>全部展开</button>
            <button type="button" className="btn btn-sm" onClick={collapseAll}>全部收起</button>
          </div>
        </div>

        {visiblePhases.map((pk) => {
          const pMeta = ROADMAP_PRIORITY_META[pk]
          const items = groupedByPhase[pk]
          return (
            <div key={pk} style={{ marginBottom: 22 }}>
              <div className="roadmap-group-header">
                <span className="roadmap-group-dot" style={{ background: pMeta.accentColor }} />
                <strong>{pMeta.phaseCode} · {pMeta.phaseTitle}</strong>
                <span style={{ color: '#6b7280', fontSize: 12.5 }}>{pMeta.phaseDesc}（{items.length} 项）</span>
              </div>

              {items.map((rec, idx) => {
                const cardKey = `${pk}-${idx}`
                const isOpen = expandedCards.has(cardKey)
                const catLabel = ROADMAP_CATEGORY_META[rec.category] || rec.category
                const tables = rec.affected_tables || []
                const scripts = rec.affected_scripts || []
                const showAllTables = expandedTables.has(cardKey)
                const shownTables = showAllTables ? tables : tables.slice(0, 8)
                const { intro, steps } = parseProposalSteps(rec.proposal)

                return (
                  <div
                    key={cardKey}
                    className="roadmap-card"
                    style={{ borderLeftColor: pMeta.accentColor }}
                  >
                    <div className="roadmap-card-head" onClick={() => toggleCard(cardKey)}>
                      <div style={{ minWidth: 0, flex: 1 }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', marginBottom: 4 }}>
                          <span className={`badge ${pMeta.badgeClass}`}>{pMeta.label}</span>
                          <span className="badge badge-neutral">{catLabel}</span>
                          <strong style={{ fontSize: 14.5, color: '#111827' }}>{rec.title}</strong>
                        </div>
                        {!isOpen && rec.expected_benefit && (
                          <div style={{ fontSize: 12.5, color: '#15803d', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                            预期收益：{rec.expected_benefit}
                          </div>
                        )}
                      </div>

                      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexShrink: 0, fontSize: 12, color: '#6b7280' }}>
                        {tables.length > 0 && <span>{tables.length} 张表</span>}
                        {scripts.length > 0 && <span>{scripts.length} 个脚本</span>}
                        <span style={{ fontWeight: 600 }}>{isOpen ? '收起 ▲' : '展开 ▼'}</span>
                      </div>
                    </div>

                    {isOpen && (
                      <div className="roadmap-card-body">
                        {rec.expected_benefit && (
                          <div className="roadmap-benefit-box">
                            <span className="roadmap-section-label" style={{ color: '#15803d' }}>预期工程收益</span>
                            <div style={{ fontSize: 13.5, color: '#14532d', marginTop: 2 }}>{rec.expected_benefit}</div>
                          </div>
                        )}

                        <div>
                          <span className="roadmap-section-label">问题上下文与实施路线</span>
                          {intro && <p className="roadmap-prose">{intro}</p>}
                          {steps.length > 0 && (
                            <ol className="roadmap-step-list">
                              {steps.map((st, sIdx) => (
                                <li key={sIdx} className="roadmap-step-item">
                                  <span className="roadmap-step-idx">{sIdx + 1}</span>
                                  <span>{st}</span>
                                </li>
                              ))}
                            </ol>
                          )}
                        </div>

                        {(tables.length > 0 || scripts.length > 0) && (
                          <div className="roadmap-assets-box">
                            {tables.length > 0 && (
                              <div className="roadmap-assets-row">
                                <span className="roadmap-assets-label">涉及数据表 ({tables.length})</span>
                                <div className="roadmap-chip-wrap">
                                  {shownTables.map((t) => (
                                    <code key={t} className="asset-chip">{t}</code>
                                  ))}
                                  {tables.length > 8 && (
                                    <button
                                      type="button"
                                      className="btn btn-sm"
                                      onClick={(e) => toggleTables(e, cardKey)}
                                    >
                                      {showAllTables ? '收起表清单' : `+${tables.length - 8} 张表`}
                                    </button>
                                  )}
                                </div>
                              </div>
                            )}

                            {scripts.length > 0 && (
                              <div className="roadmap-assets-row">
                                <span className="roadmap-assets-label">涉及脚本 ({scripts.length})</span>
                                <div className="roadmap-chip-wrap">
                                  {scripts.map((sc) => (
                                    <button
                                      key={sc}
                                      type="button"
                                      className="asset-chip-script"
                                      title="点击跳转至该脚本的问题排查清单"
                                      onClick={() => onSelectScriptName && onSelectScriptName(sc)}
                                    >
                                      {sc} ↗
                                    </button>
                                  ))}
                                </div>
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )
        })}
      </div>
    </div>
  )
}
