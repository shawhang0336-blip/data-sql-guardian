import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { ApiError, api, formatApiErrorDetail } from '../src/api/client.js'
import {
  formatRoadmapMarkdown,
  formatUtcTime,
  groupFindingsBySeverity,
  paginateItems,
  parseExecutiveSummary,
  parseProposalSteps,
} from '../src/utils/labels.js'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const SRC_DIR = path.resolve(__dirname, '../src')

function walkFiles(dir, exts) {
  const out = []
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name)
    if (entry.isDirectory()) out.push(...walkFiles(full, exts))
    else if (!exts || exts.includes(path.extname(entry.name))) out.push(full)
  }
  return out
}

test('frontend src/ obeys strict 8-directory and 4-feature symmetry', () => {
  const topDirs = fs
    .readdirSync(SRC_DIR, { withFileTypes: true })
    .filter((d) => d.isDirectory())
    .map((d) => d.name)
    .sort()
  assert.deepEqual(topDirs, ['api', 'app', 'components', 'context', 'features', 'hooks', 'styles', 'utils'])

  const featureDirs = fs
    .readdirSync(path.join(SRC_DIR, 'features'), { withFileTypes: true })
    .filter((d) => d.isDirectory())
    .map((d) => d.name)
    .sort()
  assert.deepEqual(featureDirs, ['analysis', 'platform', 'review', 'workspace'])
})

test('zero /api/ strings or fetch( calls exist outside src/api/', () => {
  const files = walkFiles(SRC_DIR, ['.js', '.jsx'])
  const apiDir = path.join(SRC_DIR, 'api')
  for (const file of files) {
    if (file.startsWith(apiDir + path.sep)) continue
    const text = fs.readFileSync(file, 'utf8')
    assert.equal(text.includes("'/api/"), false, `Forbidden /api/ literal in ${file}`)
    assert.equal(text.includes('"/api/'), false, `Forbidden /api/ literal in ${file}`)
    assert.equal(/\bfetch\s*\(/.test(text), false, `Forbidden fetch() call in ${file}`)
  }
})

test('ApiError formats FastAPI 422 arrays and non-JSON 502 cleanly', async () => {
  const err = new ApiError(422, [{ loc: ['body', 'filename'], msg: 'Field required' }])
  assert.match(err.message, /filename: Field required/)
  assert.equal(formatApiErrorDetail(null, 502), '请求失败（502）')

  const origFetch = globalThis.fetch
  let captured = null
  try {
    globalThis.fetch = async (_url, init) => {
      captured = init
      return { ok: true, status: 200, text: async () => JSON.stringify({ status: 'ok' }) }
    }
    const res = await api('/api/system/health')
    assert.equal(res.status, 'ok')
    assert.equal(captured.headers['Content-Type'], undefined)
  } finally {
    globalThis.fetch = origFetch
  }
})

test('formatUtcTime, paginateItems, and groupFindingsBySeverity behave accurately', () => {
  assert.equal(formatUtcTime(''), '—')
  const rendered = formatUtcTime('2026-11-24T02:00:00')
  assert.match(rendered, /10:00:00/)

  const paged = paginateItems(Array.from({ length: 45 }, (_, i) => i + 1), 3, 20)
  assert.equal(paged.totalPages, 3)
  assert.deepEqual(paged.items, [41, 42, 43, 44, 45])

  const counts = groupFindingsBySeverity([
    { severity: 'P0', status: 'OPEN' },
    { severity: 'P1', status: 'ADOPTED' },
    { severity: 'P2', status: 'IGNORED' },
  ])
  assert.deepEqual(counts, { P0: 1, P1: 1, P2: 1, resolved: 2, total: 3 })
})

test('roadmap summary, proposal step parsers, and markdown export handle dense Chinese LLM outputs', () => {
  const sampleSummary = [
    '整体处于高风险耦合状态，建议分四阶段治理。',
    '一、存在循环依赖。检测到 4 组环。',
    '二、存在 TRUNCATE 冲突。共 30 组清表读冲突。',
    '治理路线分四阶段：阶段 0 止血；阶段 1 分层。',
  ].join('\n\n')
  const parsed = parseExecutiveSummary(sampleSummary)
  assert.equal(parsed.lead, '整体处于高风险耦合状态，建议分四阶段治理。')
  assert.equal(parsed.sections.length, 2)
  assert.equal(parsed.sections[0].heading, '一、存在循环依赖')
  assert.match(parsed.footer, /治理路线分四阶段/)

  const parsedSteps = parseProposalSteps('背景概述。改造步骤：(1) 定义契约 (2) 保留主脚本写入 (3) 调整调度 DAG。')
  assert.equal(parsedSteps.steps.length, 3)
  assert.match(parsedSteps.intro, /背景概述/)

  const md = formatRoadmapMarkdown(
    {
      executive_summary: '摘要测试',
      architecture_recommendations: [
        {
          priority: 'P0_BLOCKER',
          category: 'ARCHITECTURE',
          title: '打破 4 组循环依赖',
          affected_tables: ['SAC_JGDEJSH_TMP'],
          affected_scripts: ['SP_SAC_JGDEJSH_TMP.sql'],
          proposal: '(1) 拆分上下游',
          expected_benefit: '支持 DAG 调度',
        },
      ],
    },
    'SAC 库',
  )
  assert.match(md, /# SAC 库/)
  assert.match(md, /P0 阻断 · 架构解耦/)
})
