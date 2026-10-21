import { useEffect, useMemo, useState } from 'react'
import { api } from '../lib/api'
import type { MediaKindState, MediaTestResult } from '../lib/types'

/**
 * 一类媒体的配置表单（图片 / 语音 / 视频共用）。
 *
 * 为什么用「字段表 + 一个渲染器」而不是三份手写 JSX：
 * 三类配置的字段结构其实同构（provider / 地址 / Key / 模型 / 参数），
 * 差别只在"有哪些参数"。写三份的结果一定是三份不一致的校验与提示。
 *
 * 两条安全约定：
 * 1. Key 不回显明文，留空即沿用（否则脱敏回显会把真 Key 覆盖掉）；
 * 2. 测试会真的产生一次调用，必须再点一次确认 —— 不能"点一下就不小心花了钱"。
 */

type Kind = 'image' | 'tts' | 'video'

interface FieldSpec {
  key: string
  label: string
  placeholder?: string
  hint?: string
  type?: 'text' | 'password' | 'number' | 'select' | 'switch' | 'textarea'
  options?: { value: string; label: string }[]
  wide?: boolean
}

const PROVIDER_LABEL: Record<string, string> = {
  auto: '自动（有 Key 走真实服务）',
  local: '本地兜底（不花钱）',
  'openai-compat': 'OpenAI 兼容接口',
  jdcloud: '京东云 modelservice',
  off: '关闭（不生成视频）',
}

const FIELDS: Record<Kind, FieldSpec[]> = {
  image: [
    { key: 'provider', label: '接入方式', type: 'select' },
    { key: 'base_url', label: '接口地址', placeholder: 'https://modelservice.jdcloud.com/v1', wide: true },
    { key: 'api_key', label: 'API Key', type: 'password', placeholder: '留空 = 沿用已保存的' },
    { key: 'model', label: '模型名', placeholder: 'image-01' },
    {
      key: 'service',
      label: '预置服务标识',
      placeholder: 'minimax_image01',
      hint: '京东云专用：控制台创建的预置服务 id，不在 /v1/models 列表里',
    },
    { key: 'negative_prompt', label: '负向提示词', placeholder: '模糊、多余手指、文字水印', wide: true, hint: '不支持该字段的服务商会忽略它（不会静默）' },
    { key: 'size', label: '尺寸', placeholder: '留空按比例推导（9:16 → 1024x1792）' },
    { key: 'seed', label: '随机种子', type: 'number', hint: '0 = 每格不同；填固定值可复现' },
    { key: 'params_json', label: '高级参数（JSON）', type: 'textarea', wide: true, placeholder: '{"steps": 30, "guidance_scale": 7}', hint: '原样合并进请求体，写错会在保存时被拒绝' },
  ],
  tts: [
    { key: 'provider', label: '接入方式', type: 'select' },
    { key: 'base_url', label: '接口地址', placeholder: 'https://api.openai.com/v1', wide: true },
    { key: 'api_key', label: 'API Key', type: 'password', placeholder: '留空 = 沿用已保存的' },
    { key: 'model', label: '模型名', placeholder: 'tts-1' },
    { key: 'voice', label: '默认音色', placeholder: 'alloy / zh-CN-XiaoxiaoNeural …' },
    { key: 'speed', label: '语速倍率', type: 'number', hint: '1.0 = 原速；每句台词还能在画布上单独覆盖' },
    { key: 'params_json', label: '高级参数（JSON）', type: 'textarea', wide: true, placeholder: '{"pitch": 1.05}', hint: '原样合并进请求体' },
  ],
  video: [
    { key: 'provider', label: '接入方式', type: 'select' },
    { key: 'base_url', label: '接口地址', placeholder: 'https://modelservice.jdcloud.com/v1', wide: true },
    {
      key: 'api_key',
      label: 'API Key',
      type: 'password',
      placeholder: '留空 = 沿用已保存的',
      hint: '留空时自动复用图片的 Key（京东云是同一个网关）',
    },
    { key: 'model', label: '默认模型名', placeholder: 'Doubao-Seedance-2.5', hint: '画布生成视频时默认用它' },
    {
      key: 'models_json',
      label: '可选模型清单（JSON）',
      type: 'textarea',
      wide: true,
      placeholder: '["wan3.0-video", {"name": "happyhorse-1.1-t2v", "params": {"resolution": "720P", "ratio": "16:9"}, "note": "文生视频"}]',
      hint: '画布上可按镜头切换的模型；对象写法可带 params（该模型专属参数，如 ratio）与 note 说明',
    },
    {
      key: 'duration',
      label: '单片段时长',
      type: 'select',
      options: [
        { value: '5', label: '5 秒' },
        { value: '10', label: '10 秒' },
      ],
      hint: '服务商只认这两个值，别的值会在本地就被拦下',
    },
    {
      key: 'mode',
      label: '生成模式',
      type: 'select',
      options: [
        { value: 'std', label: '标准（便宜）' },
        { value: 'pro', label: '专家（更贵、更好）' },
      ],
    },
    { key: 'resolution', label: '分辨率', placeholder: '留空 = 服务端默认（如 720p）' },
    { key: 'watermark', label: '服务商水印', type: 'switch', hint: '关掉更干净；部分服务商强制加' },
    { key: 'params_json', label: '高级参数（JSON）', type: 'textarea', wide: true, placeholder: '{"cfg_scale": 0.5}', hint: '合并进 parameters 字段' },
  ],
}

const KIND_TITLE: Record<Kind, string> = { image: '图片', tts: '语音', video: '视频' }

const INPUT =
  'w-full rounded-lg bg-[#0f1420] border border-[#243049] px-3 py-2 text-[12.5px] text-[#dfe7f5] outline-none focus:border-[#4f6bff]/70 placeholder:text-[#4a5670]'

export default function MediaConfigSection({
  kind,
  state,
  onSaved,
}: {
  kind: Kind
  state: MediaKindState
  onSaved: () => void
}) {
  const fields = useMemo(() => FIELDS[kind], [kind])
  const [values, setValues] = useState<Record<string, any>>({})
  const [apiKey, setApiKey] = useState('')
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [test, setTest] = useState<MediaTestResult | null>(null)
  const [needConfirm, setNeedConfirm] = useState(false)

  // 切换类别/重新拉取后重置草稿（api_key 永远从空开始，不回显明文）
  useEffect(() => {
    const init: Record<string, any> = {}
    for (const f of fields) {
      const v = state.values?.[f.key]
      init[f.key] = v === undefined || v === null ? (f.type === 'switch' ? false : '') : v
    }
    setValues(init)
    setApiKey('')
    setTest(null)
    setNeedConfirm(false)
    setMsg(null)
    setErr(null)
  }, [kind, fields, state.values])

  const patch = (k: string, v: any) => setValues((s) => ({ ...s, [k]: v }))

  const payloadValues = () => {
    const out: Record<string, any> = { ...values }
    if (apiKey.trim()) out.api_key = apiKey.trim()
    else delete out.api_key
    return out
  }

  const save = async () => {
    setSaving(true)
    setErr(null)
    setMsg(null)
    try {
      const r = await api.saveMediaConfig(kind, payloadValues())
      setMsg(r.message)
      setApiKey('')
      onSaved()
    } catch (e: any) {
      setErr(e.message)
    } finally {
      setSaving(false)
    }
  }

  const runTest = async (confirm: boolean) => {
    setTesting(true)
    setErr(null)
    setTest(null)
    try {
      const r = await api.testMediaConfig(kind, { confirm_cost: confirm, values: payloadValues() })
      setTest(r)
      setNeedConfirm(r.stage === 'need_confirm')
    } catch (e: any) {
      setErr(e.message)
    } finally {
      setTesting(false)
    }
  }

  return (
    <div className="space-y-4">
      <div
        className={`rounded-lg border px-3 py-2.5 text-[11.5px] ${
          state.ready.ready
            ? 'border-emerald-500/35 bg-emerald-500/10 text-emerald-200'
            : 'border-amber-500/35 bg-amber-500/10 text-amber-200'
        }`}
      >
        <b>{KIND_TITLE[kind]}</b> · {state.ready.ready ? '已就绪' : '未就绪'}
        <div className="opacity-85 mt-0.5">{state.ready.hint}</div>
      </div>

      <div className="grid grid-cols-2 gap-3">
        {fields.map((f) => (
          <div key={f.key} className={f.wide ? 'col-span-2' : 'col-span-2 sm:col-span-1'}>
            <label className="text-[11px] text-[#7c8aa5] block mb-1.5">{f.label}</label>

            {f.type === 'select' ? (
              <select
                className={INPUT}
                value={String(values[f.key] ?? '')}
                onChange={(e) => patch(f.key, e.target.value)}
              >
                {(f.options ?? state.providers.map((p) => ({ value: p, label: PROVIDER_LABEL[p] ?? p }))).map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            ) : f.type === 'switch' ? (
              <button
                onClick={() => patch(f.key, !values[f.key])}
                className={`text-[11.5px] px-3 py-1.5 rounded-lg border transition-colors ${
                  values[f.key]
                    ? 'border-[#4f6bff]/60 bg-[#4f6bff]/15 text-[#c9d5ea]'
                    : 'border-[#243049] bg-[#0f1420] text-[#8d9cb8]'
                }`}
              >
                {values[f.key] ? '开启' : '关闭'}
              </button>
            ) : f.type === 'textarea' ? (
              <textarea
                rows={3}
                className={`${INPUT} resize-y font-mono`}
                value={String(values[f.key] ?? '')}
                placeholder={f.placeholder}
                spellCheck={false}
                onChange={(e) => patch(f.key, e.target.value)}
              />
            ) : (
              <input
                className={INPUT}
                type={f.type === 'password' ? 'password' : f.type === 'number' ? 'text' : 'text'}
                inputMode={f.type === 'number' ? 'decimal' : undefined}
                value={f.key === 'api_key' ? apiKey : String(values[f.key] ?? '')}
                placeholder={
                  f.key === 'api_key' && state.values?.api_key_set
                    ? `已保存 ${state.values.api_key_masked}（留空则沿用）`
                    : f.placeholder
                }
                spellCheck={false}
                autoComplete="off"
                onChange={(e) => (f.key === 'api_key' ? setApiKey(e.target.value) : patch(f.key, e.target.value))}
              />
            )}

            {f.hint && <div className="text-[10px] text-[#4c5975] mt-1 leading-relaxed">{f.hint}</div>}
          </div>
        ))}
      </div>

      {needConfirm && (
        <div className="rounded-lg border border-orange-500/40 bg-orange-500/10 px-3 py-2.5 text-[11.5px] text-orange-200">
          测试会真实调用一次{kind === 'image' ? '生图' : kind === 'tts' ? '语音合成' : '视频接口'}（会产生费用）。
          {kind === 'video' ? '视频只做鉴权探测，不提交生成任务。' : ''}
          <button
            onClick={() => runTest(true)}
            className="ml-2 text-[11px] px-2.5 py-1 rounded-md bg-orange-500/25 hover:bg-orange-500/35 text-orange-100"
          >
            确认并测试
          </button>
        </div>
      )}

      {test && test.stage !== 'need_confirm' && (
        <div
          className={`rounded-lg border px-3 py-2.5 text-[11.5px] break-words ${
            test.ok
              ? 'border-emerald-500/35 bg-emerald-500/10 text-emerald-200'
              : 'border-rose-500/40 bg-rose-500/10 text-rose-200'
          }`}
        >
          {test.ok ? (
            <>
              连接成功{test.latency_ms ? ` · ${test.latency_ms}ms` : ''}
              {test.message ? ` · ${test.message}` : ''}
              {test.http_status ? ` · HTTP ${test.http_status}` : ''}
            </>
          ) : (
            <>
              未通过：{test.error}
              {test.hint && <div className="opacity-80 mt-1">{test.hint}</div>}
            </>
          )}
          {test.preview && (
            <img src={test.preview} alt="测试出图" className="mt-2 max-h-[160px] rounded-md border border-white/10" />
          )}
        </div>
      )}

      {msg && (
        <div className="rounded-lg border border-[#4f6bff]/40 bg-[#4f6bff]/10 text-[11.5px] text-[#c9d5ea] px-3 py-2.5">
          {msg}
        </div>
      )}
      {err && (
        <div className="rounded-lg border border-rose-500/40 bg-rose-500/10 text-[11.5px] text-rose-200 px-3 py-2.5">
          {err}
        </div>
      )}

      <div className="flex gap-2 pt-1">
        <button
          onClick={() => runTest(false)}
          disabled={testing || saving}
          className="flex-1 rounded-lg border border-[#243049] bg-[#0f1420] hover:border-[#4f6bff]/60 text-[12.5px] text-[#c9d5ea] py-2.5 disabled:opacity-50 transition-colors"
        >
          {testing ? '测试中…' : '测试连接'}
        </button>
        <button
          onClick={save}
          disabled={saving || testing}
          className="flex-1 rounded-lg bg-[#4f6bff] hover:bg-[#3f5aee] text-[12.5px] font-medium text-white py-2.5 disabled:opacity-50 transition-colors"
        >
          {saving ? '保存中…' : '保存并启用'}
        </button>
      </div>
      <div className="text-[10.5px] text-[#5f6d87] leading-relaxed">
        {state.ready.mode === 'off' ? '关闭状态下不会产生任何视频调用，成片仍会正常渲染（静态图 + 运镜）。' : ''}
        保存写入 backend/.env，重启后依然有效；已有任务不会被改写。
      </div>
    </div>
  )
}
