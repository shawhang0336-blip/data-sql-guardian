import React, { useEffect, useState } from 'react'
import { PageHeader } from '../../components/PageHeader.jsx'
import { useProjectContext } from '../../context/ProjectContext.jsx'
import { useAsync } from '../../hooks/useAsync.js'
import {
  fetchSettings,
  testLlmSettings,
  updateBudgetSettings,
  updateLlmSettings,
} from '../../api/platform.js'

export default function SettingsPage() {
  const { pushToast } = useProjectContext()
  const { busy, error, setError, run } = useAsync()
  const [llm, setLlm] = useState({ llm_provider: 'scripted', llm_base_url: '', llm_model: '', llm_timeout: 120, llm_api_key: '', has_api_key: false })
  const [budget, setBudget] = useState({ max_retries: 3, max_llm_concurrency: 4, max_llm_rpm: 60, max_calls_per_script: 8, max_llm_calls: 600 })
  const [testResult, setTestResult] = useState(null)

  useEffect(() => {
    fetchSettings()
      .then((res) => {
        if (res?.llm) setLlm((prev) => ({ ...prev, ...res.llm, llm_api_key: '' }))
        if (res?.budget) setBudget(res.budget)
      })
      .catch((e) => setError(e?.message || '读取系统配置失败'))
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
      pushToast('success', '模型 Provider 配置已保存生效')
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
      pushToast('success', 'BudgetGuard 与限流并发上限已更新')
    })
  }

  const handleTest = () =>
    run(async () => {
      const res = await testLlmSettings()
      setTestResult(res)
      pushToast(res.ok ? 'success' : 'error', res.ok ? '模型连通性验证通过' : `连通测试失败：${res.message || ''}`)
    })

  return (
    <div>
      <PageHeader
        title="系统与模型运行时配置"
        subtitle="热更新大模型 Provider（接口仅脱敏回显，不回传明文 Key）、TokenBucket 限流与 BudgetGuard 熔断预算"
      />

      {error && <div className="alert alert-error">{error}</div>}

      <div className="grid-2">
        <div className="card">
          <h3 className="card-title">
            <span>大模型 Provider 连接设置</span>
            <span className={`badge ${llm.has_api_key ? 'badge-adopted' : 'badge-neutral'}`}>
              {llm.has_api_key ? 'API Key 已就绪（脱敏保护）' : '当前无外部 Key'}
            </span>
          </h3>
          <form onSubmit={handleSaveLlm} style={{ display: 'grid', gap: 12 }}>
            <label>
              <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 4 }}>运行模式 (Provider)</div>
              <select className="select" value={llm.llm_provider} onChange={(e) => setLlm({ ...llm, llm_provider: e.target.value })}>
                <option value="scripted">scripted · 纯离线确定性规则引擎模式</option>
                <option value="deepseek">deepseek · DeepSeek Tool-Call</option>
                <option value="openai">openai · OpenAI / 兼容网关</option>
              </select>
            </label>
            <label>
              <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 4 }}>API Base URL</div>
              <input className="input" value={llm.llm_base_url} onChange={(e) => setLlm({ ...llm, llm_base_url: e.target.value })} />
            </label>
            <div className="grid-2">
              <label>
                <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 4 }}>模型名称</div>
                <input className="input" value={llm.llm_model} onChange={(e) => setLlm({ ...llm, llm_model: e.target.value })} />
              </label>
              <label>
                <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 4 }}>请求超时（秒）</div>
                <input className="input" type="number" min="10" max="600" value={llm.llm_timeout} onChange={(e) => setLlm({ ...llm, llm_timeout: e.target.value })} />
              </label>
            </div>
            <label>
              <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 4 }}>更换 API Key（留空表示维持现状）</div>
              <input className="input" type="password" value={llm.llm_api_key} onChange={(e) => setLlm({ ...llm, llm_api_key: e.target.value })} placeholder="••••••••" />
            </label>
            <div style={{ display: 'flex', gap: 8 }}>
              <button type="submit" className="btn btn-primary" disabled={busy}>保存模型设置</button>
              <button type="button" className="btn" disabled={busy} onClick={handleTest}>测试模型连通性</button>
            </div>
          </form>

          {testResult && (
            <div className={`alert ${testResult.ok ? 'alert-info' : 'alert-error'}`} style={{ marginTop: 14, marginBottom: 0 }}>
              <strong>{testResult.ok ? '连通正常' : `异常（${testResult.status || 'ERROR'}）`}：</strong>
              {testResult.message || `Provider=${testResult.provider}, Model=${testResult.model}`}
            </div>
          )}
        </div>

        <div className="card">
          <h3 className="card-title">BudgetGuard 成本熔断与限流并发护栏</h3>
          <form onSubmit={handleSaveBudget} style={{ display: 'grid', gap: 12 }}>
            <label>
              <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 4 }}>全局最大并发信号量（max_llm_concurrency）</div>
              <input className="input" type="number" min="1" max="32" value={budget.max_llm_concurrency} onChange={(e) => setBudget({ ...budget, max_llm_concurrency: e.target.value })} />
            </label>
            <label>
              <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 4 }}>RPM 令牌桶上限（max_llm_rpm）</div>
              <input className="input" type="number" min="1" max="1000" value={budget.max_llm_rpm} onChange={(e) => setBudget({ ...budget, max_llm_rpm: e.target.value })} />
            </label>
            <label>
              <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 4 }}>单脚本深度剖析调用配额（max_calls_per_script）</div>
              <input className="input" type="number" min="1" max="100" value={budget.max_calls_per_script} onChange={(e) => setBudget({ ...budget, max_calls_per_script: e.target.value })} />
            </label>
            <label>
              <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 4 }}>单次全仓运行总调用上限（max_llm_calls）</div>
              <input className="input" type="number" min="1" max="5000" value={budget.max_llm_calls} onChange={(e) => setBudget({ ...budget, max_llm_calls: e.target.value })} />
            </label>
            <div>
              <button type="submit" className="btn btn-primary" disabled={busy}>保存预算护栏</button>
            </div>
          </form>
        </div>
      </div>
    </div>
  )
}
