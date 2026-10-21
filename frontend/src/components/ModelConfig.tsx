import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import type { MediaConfigState, ModelPreset, ModelStatus, ModelTestResult } from '../lib/types'
import MediaConfigSection from './MediaConfigSection'

interface Props {
  open: boolean
  onClose: () => void
  onSaved: (status: ModelStatus) => void
}

type ConfigTab = 'text' | 'image' | 'tts' | 'video'

/** 接入方式三选一：自动 / 强制真实 / 演示 */
const ACCESS_MODES = [
  { id: 'auto', label: '自动', hint: '填了 Key 就用真实模型，没填就用内置样例' },
  { id: 'openai-compat', label: '真实模型', hint: '强制调用外部接口（OpenAI 兼容协议）' },
  { id: 'mock', label: '演示模式', hint: '不调用外部接口，用技能包内置样例，零成本' },
]

const TABS: { id: ConfigTab; label: string; hint: string }[] = [
  { id: 'text', label: '文本', hint: '剧本 / 分镜 / 提示词 —— 所有"用语言思考"的阶段' },
  { id: 'image', label: '图片', hint: '分镜画面' },
  { id: 'tts', label: '语音', hint: '角色配音' },
  { id: 'video', label: '视频', hint: '镜头片段（按次计费）' },
]

export default function ModelConfig({ open, onClose, onSaved }: Props) {
  const [loading, setLoading] = useState(false)
  const [presets, setPresets] = useState<ModelPreset[]>([])
  const [status, setStatus] = useState<ModelStatus | null>(null)
  const [envFile, setEnvFile] = useState('')
  const [envExists, setEnvExists] = useState(false)
  const [tab, setTab] = useState<ConfigTab>('text')
  const [media, setMedia] = useState<MediaConfigState | null>(null)

  const [provider, setProvider] = useState('auto')
  const [baseUrl, setBaseUrl] = useState('')
  const [model, setModel] = useState('')
  const [fallbacks, setFallbacks] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [temperature, setTemperature] = useState('0.7')

  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<ModelTestResult | null>(null)
  const [listing, setListing] = useState(false)
  const [modelList, setModelList] = useState<string[] | null>(null)
  const [listError, setListError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [savedMsg, setSavedMsg] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const loadMedia = () => {
    api
      .mediaConfig()
      .then(setMedia)
      .catch((e: any) => setError(`读取媒体配置失败：${e.message}`))
  }

  useEffect(() => {
    if (!open) return
    setLoading(true)
    setError(null)
    setTestResult(null)
    setSavedMsg(null)
    setModelList(null)
    setListError(null)
    loadMedia()
    api
      .modelConfig()
      .then((r) => {
        setPresets(r.presets)
        setStatus(r.status)
        setEnvFile(r.env_file)
        setEnvExists(r.env_file_exists)
        setProvider(r.status.configured_provider || 'auto')
        setBaseUrl(r.status.base_url)
        setModel(r.status.model)
        setFallbacks(r.status.fallback_raw || '')
        setApiKey('') // 不回显明文 Key，留空即沿用已保存的
      })
      .catch((e: any) => setError(`读取配置失败：${e.message}`))
      .finally(() => setLoading(false))
  }, [open])

  if (!open) return null

  const payload = () => ({
    provider,
    base_url: baseUrl.trim(),
    api_key: apiKey.trim() || undefined,
    model: model.trim(),
    temperature: Number(temperature) || 0.7,
    fallback_models: fallbacks.trim(),
  })

  /** 把某个模型加进备用链（列表里点「+备用」时用），已存在就不重复加 */
  const addFallback = (name: string) => {
    const items = fallbacks.split(',').map((s) => s.trim()).filter(Boolean)
    if (name === model.trim() || items.includes(name)) return
    setFallbacks([...items, name].join(','))
  }

  const applyPreset = (p: ModelPreset) => {
    setProvider(p.id === 'ollama' ? 'openai-compat' : 'auto')
    if (p.base_url) setBaseUrl(p.base_url)
    if (p.model) setModel(p.model)
    setTestResult(null)
    setSavedMsg(null)
    setModelList(null)
    setListError(null)
  }

  const doTest = async () => {
    setTesting(true)
    setTestResult(null)
    setError(null)
    try {
      setTestResult(await api.testModelConfig(payload()))
    } catch (e: any) {
      setTestResult({ ok: false, error: e.message })
    } finally {
      setTesting(false)
    }
  }

  const doList = async () => {
    setListing(true)
    setModelList(null)
    setListError(null)
    try {
      const r = await api.listModels({ base_url: baseUrl.trim(), api_key: apiKey.trim() || undefined })
      if (r.ok && r.models) setModelList(r.models)
      else setListError(r.error || '拉取失败')
    } catch (e: any) {
      setListError(e.message)
    } finally {
      setListing(false)
    }
  }

  const doSave = async () => {
    setSaving(true)
    setError(null)
    setSavedMsg(null)
    try {
      const r = await api.saveModelConfig(payload())
      setStatus(r.status)
      setEnvExists(true)
      setSavedMsg(r.message)
      onSaved(r.status)
    } catch (e: any) {
      setError(`保存失败：${e.message}`)
    } finally {
      setSaving(false)
    }
  }

  const inputCls =
    'w-full rounded-lg bg-[#0f1420] border border-[#243049] px-3 py-2 text-[12.5px] text-[#dfe7f5] outline-none focus:border-[#4f6bff]/70 placeholder:text-[#4a5670]'

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      {/* 遮罩 */}
      <div className="absolute inset-0 bg-black/55 backdrop-blur-[1px]" onClick={onClose} />

      <div className="relative w-[520px] max-w-[94vw] h-full bg-[#0b0f17] border-l border-[#1e2839] flex flex-col slide-in-right">
        {/* 头部 */}
        <div className="px-5 py-4 border-b border-[#1e2839] flex items-start gap-3">
          <div className="flex-1">
            <div className="text-[14.5px] font-semibold">模型配置</div>
            <div className="text-[11px] text-[#7c8aa5] mt-1">
              四类能力各自独立配置，互不影响。保存后立即生效，无需重启。
            </div>
          </div>
          <button onClick={onClose} className="text-[#7c8aa5] hover:text-[#c9d5ea] text-[18px] leading-none px-1">
            ×
          </button>
        </div>

        {/* 四类能力页签 */}
        <div className="px-5 pt-3 flex items-center gap-1.5 border-b border-[#1e2839] pb-3">
          {TABS.map((t) => {
            const ready = t.id === 'text' ? status?.mode === 'real' : media?.kinds?.[t.id]?.ready.ready
            return (
              <button
                key={t.id}
                onClick={() => setTab(t.id)}
                title={t.hint}
                className={`relative text-[12px] px-3 py-1.5 rounded-lg transition-colors ${
                  tab === t.id ? 'bg-[#1a2233] text-[#dbe4f5]' : 'text-[#6f7d99] hover:text-[#a8b6d1]'
                }`}
              >
                {t.label}
                <span
                  className={`ml-1.5 inline-block w-1.5 h-1.5 rounded-full align-middle ${
                    ready ? 'bg-emerald-400' : 'bg-[#3a465e]'
                  }`}
                />
              </button>
            )
          })}
          <span className="ml-auto text-[10px] text-[#4c5975]">{TABS.find((t) => t.id === tab)?.hint}</span>
        </div>

        {tab !== 'text' && (
          <div className="flex-1 overflow-y-auto px-5 py-4">
            {media?.kinds?.[tab] ? (
              <MediaConfigSection kind={tab} state={media.kinds[tab]} onSaved={loadMedia} />
            ) : (
              <div className="text-[12px] text-[#5f6d87]">读取配置中…</div>
            )}
          </div>
        )}

        {tab === 'text' && (
          <>
        {/* 当前状态 */}
        {status && (
          <div className="px-5 pt-4">
            <div
              className={`rounded-lg border px-3 py-2.5 text-[11.5px] ${
                status.mode === 'real'
                  ? 'border-emerald-500/35 bg-emerald-500/10 text-emerald-200'
                  : 'border-amber-500/35 bg-amber-500/10 text-amber-200'
              }`}
            >
              {status.mode === 'real' ? (
                <>
                  当前生效：<b>{status.model}</b> · 真实模型
                  <div className="text-[10.5px] opacity-80 mt-0.5 break-all">{status.base_url}</div>
                  <div className="text-[10.5px] opacity-80 mt-0.5">
                    {status.fallback_models?.length
                      ? `备用：${status.fallback_models.join(' → ')}`
                      : '未配备用模型（主模型不可用时任务会失败）'}
                  </div>
                </>
              ) : (
                <>当前生效：演示模式（内置样例，不调用外部接口）</>
              )}
            </div>
          </div>
        )}

        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-5">
          {loading && <div className="text-[12px] text-[#5f6d87]">读取配置中…</div>}

          {/* 服务商预设 */}
          <div>
            <div className="text-[11px] text-[#7c8aa5] mb-2">服务商（一键填充地址与模型名）</div>
            <div className="flex flex-wrap gap-1.5">
              {presets.map((p) => (
                <button
                  key={p.id}
                  onClick={() => applyPreset(p)}
                  title={p.hint}
                  className="text-[11.5px] px-2.5 py-1.5 rounded-lg border border-[#243049] bg-[#0f1420] hover:border-[#4f6bff]/60 hover:text-[#c9d5ea] text-[#9fb0cc] transition-colors"
                >
                  {p.label}
                </button>
              ))}
            </div>
          </div>

          {/* 接入方式 */}
          <div>
            <div className="text-[11px] text-[#7c8aa5] mb-2">接入方式</div>
            <div className="grid grid-cols-3 gap-1.5">
              {ACCESS_MODES.map((m) => (
                <button
                  key={m.id}
                  onClick={() => setProvider(m.id)}
                  title={m.hint}
                  className={`rounded-lg border px-2 py-2 text-[11.5px] transition-colors ${
                    provider === m.id
                      ? 'border-[#4f6bff]/60 bg-[#4f6bff]/12 text-[#c9d5ea]'
                      : 'border-[#243049] bg-[#0f1420] text-[#8d9cb8] hover:border-[#2f3d5a]'
                  }`}
                >
                  {m.label}
                </button>
              ))}
            </div>
            <div className="text-[10.5px] text-[#5f6d87] mt-1.5">
              {ACCESS_MODES.find((m) => m.id === provider)?.hint}
            </div>
          </div>

          {/* 接口地址 */}
          <div>
            <label className="text-[11px] text-[#7c8aa5] block mb-1.5">接口地址（Base URL）</label>
            <input
              className={inputCls}
              value={baseUrl}
              onChange={(e) => {
                setBaseUrl(e.target.value)
                setModelList(null)
                setListError(null)
              }}
              placeholder="https://api.deepseek.com/v1"
              spellCheck={false}
            />
            <div className="text-[10.5px] text-[#5f6d87] mt-1">
              填到 /v1 即可；直接粘贴 …/chat/completions 也行，后端会自动裁掉
            </div>
          </div>

          {/* API Key */}
          <div>
            <label className="text-[11px] text-[#7c8aa5] block mb-1.5">API Key</label>
            <input
              className={inputCls}
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder={status?.api_key_set ? `已保存 ${status.api_key_masked}（留空则沿用）` : 'sk-…'}
              spellCheck={false}
              autoComplete="off"
            />
          </div>

          {/* 模型名 */}
          <div className="grid grid-cols-[1fr_96px] gap-3">
            <div>
              <label className="text-[11px] text-[#7c8aa5] block mb-1.5">模型名（Model）</label>
              <div className="flex gap-2">
                <input
                  className={`${inputCls} flex-1 min-w-0`}
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                  placeholder="deepseek-flash"
                  spellCheck={false}
                />
                <button
                  onClick={doList}
                  disabled={listing}
                  title="向服务商拉取当前可用的模型名（模型名变动比文档快）"
                  className="shrink-0 rounded-lg border border-[#243049] bg-[#0f1420] hover:border-[#4f6bff]/60 text-[11px] text-[#9fb0cc] hover:text-[#c9d5ea] px-2.5 disabled:opacity-50 transition-colors"
                >
                  {listing ? '…' : '拉取'}
                </button>
              </div>
            </div>
            <div>
              <label className="text-[11px] text-[#7c8aa5] block mb-1.5">温度</label>
              <input
                className={inputCls}
                value={temperature}
                onChange={(e) => setTemperature(e.target.value)}
                placeholder="0.7"
                inputMode="decimal"
              />
            </div>
          </div>

          {/* 备用模型链 */}
          <div>
            <label className="text-[11px] text-[#7c8aa5] block mb-1.5">
              备用模型（主模型不可用时自动顶上，可选）
            </label>
            <input
              className={inputCls}
              value={fallbacks}
              onChange={(e) => setFallbacks(e.target.value)}
              placeholder="例如：DeepSeek-V4-Flash,deepseek-v4-pro"
              spellCheck={false}
            />
            <div className="text-[10.5px] text-[#5f6d87] mt-1 leading-relaxed">
              逗号分隔，按顺序尝试。只写模型名 = 同一个接口；写成{' '}
              <code className="text-[#8d9cb8]">模型名@https://别的地址/v1</code> 就能连接口一起换。
              <br />
              配它是有必要的：中转站会给某个模型名恒返 503，换个同系列的名字往往就通了，
              否则整轮长任务白跑。
            </div>
          </div>

          {/* 拉取到的模型列表 */}
          {listError && (
            <div className="text-[10.5px] text-rose-300 -mt-3 break-words">拉取模型列表失败：{listError}</div>
          )}
          {modelList && (
            <div className="-mt-3">
              <div className="text-[10.5px] text-[#5f6d87] mb-1.5">
                服务商当前可用 {modelList.length} 个模型，点一个填入：
              </div>
              <div className="max-h-[150px] overflow-y-auto rounded-lg border border-[#243049] bg-[#0f1420] p-2 flex flex-wrap gap-1.5">
                {modelList.length === 0 && (
                  <span className="text-[10.5px] text-[#5f6d87]">服务商没有返回任何模型</span>
                )}
                {modelList.map((m) => (
                  <span key={m} className="inline-flex items-stretch">
                    <button
                      onClick={() => {
                        setModel(m)
                        setTestResult(null)
                      }}
                      className={`text-[10.5px] px-2 py-1 rounded-l-md border border-r-0 transition-colors ${
                        model === m
                          ? 'border-[#4f6bff]/60 bg-[#4f6bff]/15 text-[#c9d5ea]'
                          : 'border-[#243049] hover:border-[#4f6bff]/50 text-[#9fb0cc] hover:text-[#c9d5ea]'
                      }`}
                    >
                      {m}
                    </button>
                    <button
                      onClick={() => addFallback(m)}
                      title="加入备用模型链"
                      className="text-[10.5px] px-1.5 py-1 rounded-r-md border border-[#243049] text-[#5f6d87] hover:text-[#9fb0cc] hover:border-[#4f6bff]/50 transition-colors"
                    >
                      +备用
                    </button>
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* 结果 */}
          {testResult && (
            <div
              className={`rounded-lg border px-3 py-2.5 text-[11.5px] whitespace-pre-wrap break-words ${
                testResult.ok
                  ? 'border-emerald-500/35 bg-emerald-500/10 text-emerald-200'
                  : 'border-rose-500/40 bg-rose-500/10 text-rose-200'
              }`}
            >
              {testResult.ok ? (
                <>
                  连接成功 · {testResult.latency_ms}ms · 模型回：{testResult.reply || '（空）'}
                  <div className="text-[10.5px] opacity-80 mt-0.5 break-all">
                    {testResult.endpoint} · 返回模型名 {testResult.model}
                  </div>
                </>
              ) : (
                <>
                  连接失败：{testResult.error}
                  {testResult.fallbacks?.some((f) => f.ok) && (
                    <div className="mt-1">不过备用模型是通的，任务仍能跑（见下）。</div>
                  )}
                </>
              )}

              {!!testResult.fallbacks?.length && (
                <div className="mt-2 pt-2 border-t border-white/10 space-y-0.5">
                  <div className="text-[10.5px] opacity-80">备用模型探测：</div>
                  {testResult.fallbacks.map((f) => (
                    <div key={f.model} className="text-[10.5px]">
                      {f.ok ? '✔' : '✘'} {f.model}
                      {f.ok ? ` · ${f.latency_ms}ms` : ` · ${f.error || '不可用'}`}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {savedMsg && (
            <div className="rounded-lg border border-[#4f6bff]/40 bg-[#4f6bff]/10 text-[11.5px] text-[#c9d5ea] px-3 py-2.5">
              {savedMsg}
            </div>
          )}

          {error && (
            <div className="rounded-lg border border-rose-500/40 bg-rose-500/10 text-[11.5px] text-rose-200 px-3 py-2.5">
              {error}
            </div>
          )}
        </div>

        {/* 底部操作 */}
        <div className="px-5 py-4 border-t border-[#1e2839]">
          <div className="flex gap-2">
            <button
              onClick={doTest}
              disabled={testing || saving}
              className="flex-1 rounded-lg border border-[#243049] bg-[#0f1420] hover:border-[#4f6bff]/60 text-[12.5px] text-[#c9d5ea] py-2.5 disabled:opacity-50 transition-colors"
            >
              {testing ? '测试中…' : '测试连接'}
            </button>
            <button
              onClick={doSave}
              disabled={saving || testing}
              className="flex-1 rounded-lg bg-[#4f6bff] hover:bg-[#3f5aee] text-[12.5px] font-medium text-white py-2.5 disabled:opacity-50 transition-colors"
            >
              {saving ? '保存中…' : '保存并启用'}
            </button>
          </div>
          <div className="text-[10.5px] text-[#5f6d87] mt-2.5 break-all">
            配置写入 {envFile || 'backend/.env'}
            {envExists ? '（已存在，将更新对应行）' : '（首次保存时创建）'}，重启后依然有效。
          </div>
        </div>
          </>
        )}
      </div>
    </div>
  )
}
