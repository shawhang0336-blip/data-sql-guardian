import React, { useEffect, useState } from 'react'
import { PageHeader } from '../../components/PageHeader.jsx'
import { useAsync } from '../../hooks/useAsync.js'
import {
  fetchSettings,
  testLlmSettings,
  updateBudgetSettings,
  updateLlmSettings,
} from '../../api/platform.js'

export default function SettingsPage() {
  const { busy, error, setError, run } = useAsync()
  const [llm, setLlm] = useState({ llm_provider: 'scripted', llm_base_url: '', llm_model: '', llm_timeout: 120, llm_api_key: '', has_api_key: false })
  const [budget, setBudget] = useState({ max_retries: 3, max_llm_concurrency: 4, max_llm_rpm: 60, max_calls_per_script: 8, max_llm_calls: 600 })
  const [testResult, setTestResult] = useState(null)
  const [notice, setNotice] = useState('')

  useEffect(() => {
    fetchSettings()
      .then((res) => {
        if (res?.llm) setLlm((prev) => ({ ...prev, ...res.llm, llm_api_key: '' }))
        if (res?.budget) setBudget(res.budget)
      })
      .catch((e) => setError(e?.message || '读取配置失败'))
  }, [])

  const handleSaveLlm = (e) => {
    e.preventDefault()
    run(async () => {
      const res = await updateLlmSettings({
        llm_provider: llm.llm_provider,
        llm_base_url: llm.llm_base_url,
        llm_model: llm.llm_model,
        llm_timeout: Number(llm.llm_timeout) || 120,
        ...(llm.llm_api_key.trim() ? { llm_api_key: llm.llm_api_key.trim() } : {}),
      })
      setLlm((prev) => ({ ...prev, ...res.llm, llm_api_key: '' }))
      setNotice('模型配置已持久化生效。')
    })
  }

  const handleSaveBudget = (e) => {
    e.preventDefault()
    run(async () => {
      const res = await updateBudgetSettings({
        max_retries: Number(budget.max_retries),
        max_llm_concurrency: Number(budget.max_llm_concurrency),
        max_llm_rpm: Number(budget.max_llm_rpm),
        max_calls_per_script: Number(budget.max_calls_per_script),
        max_llm_calls: Number(budget.max_llm_calls),
      })
      setBudget(res.budget)
      setNotice('并发与预算护栏已更新。')
    })
  }

  const handleTest = () =>
    run(async () => {
      const res = await testLlmSettings()
      setTestResult(res)
    })

  return (
    <div>
      <PageHeader
        title="系统与模型运行时配置"
        subtitle="独立管理 LLM 模型配置（密钥脱敏）、TokenBucket 限流并发与 BudgetGuard 成本熔断上限"
      />

      {error && <div className="alert alert-error">{error}</div>}
      {notice && <div className="alert alert-info">{notice}</div>}

      <div className="grid-2">
        <div className="card">
          <h3 style={{ marginTop: 0 }}>大模型 Provider 配置</h3>
          <form onSubmit={handleSaveLlm} style={{ display: 'grid', gap: 10 }}>
            <label>
              Provider 模式：
              <select className="select" value={llm.llm_provider} onChange={(e) => setLlm({ ...llm, llm_provider: e.target.value })}>
                <option value="scripted">scripted（纯离线规则模式）</option>
                <option value="deepseek">deepseek（OpenAI Tool-Call 兼容）</option>
                <option value="openai">openai</option>
              </select>
            </label>
            <label>
              Base URL：
              <input className="input" value={llm.llm_base_url} onChange={(e) => setLlm({ ...llm, llm_base_url: e.target.value })} />
            </label>
            <label>
              模型名称：
              <input className="input" value={llm.llm_model} onChange={(e) => setLlm({ ...llm, llm_model: e.target.value })} />
            </label>
            <label>
              API Key（已配置：{llm.has_api_key ? '是 · 脱敏保护中' : '否'}）：
              <input className="input" type="password" value={llm.llm_api_key} onChange={(e) => setLlm({ ...llm, llm_api_key: e.target.value })} placeholder="留空表示保持原有 Key" />
            </label>
            <div style={{ display: 'flex', gap: 8 }}>
              <button type="submit" className="btn btn-primary" disabled={busy}>保存模型设置</button>
              <button type="button" className="btn" disabled={busy} onClick={handleTest}>测试模型连通性</button>
            </div>
          </form>
          {testResult && (
            <div className={`alert ${testResult.ok ? 'alert-info' : 'alert-error'}`} style={{ marginTop: 12 }}>
              {JSON.stringify(testResult)}
            </div>
          )}
        </div>

        <div className="card">
          <h3 style={{ marginTop: 0 }}>BudgetGuard & 限流参数</h3>
          <form onSubmit={handleSaveBudget} style={{ display: 'grid', gap: 10 }}>
            <label>
              最大并发信号量（max_llm_concurrency）：
              <input className="input" type="number" min="1" max="32" value={budget.max_llm_concurrency} onChange={(e) => setBudget({ ...budget, max_llm_concurrency: e.target.value })} />
            </label>
            <label>
              每分钟最大请求数（max_llm_rpm）：
              <input className="input" type="number" min="1" max="1000" value={budget.max_llm_rpm} onChange={(e) => setBudget({ ...budget, max_llm_rpm: e.target.value })} />
            </label>
            <label>
              单脚本 LLM 调用上限（max_calls_per_script）：
              <input className="input" type="number" min="1" max="100" value={budget.max_calls_per_script} onChange={(e) => setBudget({ ...budget, max_calls_per_script: e.target.value })} />
            </label>
            <label>
              单次 Run 总调用上限（max_llm_calls）：
              <input className="input" type="number" min="1" max="5000" value={budget.max_llm_calls} onChange={(e) => setBudget({ ...budget, max_llm_calls: e.target.value })} />
            </label>
            <button type="submit" className="btn btn-primary" disabled={busy}>保存预算护栏</button>
          </form>
        </div>
      </div>
    </div>
  )
}
