import { useEffect, useState } from 'react'
import type { SkillDetail } from '../lib/types'

interface Props {
  skill: SkillDetail
  busy: boolean
  onCreate: (brief: Record<string, any>, presetId: string | null, channel: 'fast' | 'slow') => void
}

export default function CreatePanel({ skill, busy, onCreate }: Props) {
  const [brief, setBrief] = useState<Record<string, any>>({})
  const [channel, setChannel] = useState<'fast' | 'slow'>('fast')

  // 表单全部预填默认值 —— 用户不填任何东西也能直接开始
  useEffect(() => {
    const init: Record<string, any> = {}
    for (const inp of skill.entry_inputs) init[inp.key] = inp.default ?? ''
    setBrief(init)
  }, [skill.id])

  const fastStages = skill.stages.filter((s) => s.fast_channel)
  const gates = (channel === 'fast' ? fastStages : skill.stages).filter((s) => s.human_approval_default)

  return (
    <div className="h-full overflow-y-auto">
      <div className="max-w-[860px] mx-auto px-8 py-9">
        {/* 头部 */}
        <div className="flex items-start gap-3.5">
          <div className="text-[34px] leading-none">{skill.icon}</div>
          <div className="flex-1">
            <h1 className="text-[21px] font-semibold tracking-tight">{skill.name}</h1>
            <p className="text-[13px] text-[#8b98b4] mt-1.5 leading-relaxed">{skill.description}</p>
            <div className="flex flex-wrap gap-1.5 mt-3">
              {skill.tags.map((t) => (
                <span key={t} className="text-[10.5px] px-2 py-[3px] rounded-full bg-[#1a2233] text-[#8b98b4]">{t}</span>
              ))}
            </div>
          </div>
        </div>

        {/* 一键试用 */}
        <section className="mt-8">
          <div className="flex items-baseline gap-2 mb-3">
            <h2 className="text-[13.5px] font-medium">试一下就出效果</h2>
            <span className="text-[11px] text-[#5f6d87]">选一个示例直接跑，不用填任何东西</span>
          </div>
          <div className="grid grid-cols-3 gap-2.5">
            {skill.presets.map((p) => (
              <button
                key={p.id}
                disabled={busy}
                onClick={() => onCreate({ ...brief, ...p.brief }, p.id, channel)}
                className="group text-left rounded-xl border border-[#1e2839] bg-[#0f1420] p-3.5 hover:border-[#4f6bff]/50 hover:bg-[#121826] transition-all disabled:opacity-40"
              >
                <div className="text-[12.5px] font-medium text-[#c9d5ea] group-hover:text-white transition-colors">
                  {p.label}
                </div>
                <div className="text-[11px] text-[#6f7d99] mt-1.5 line-clamp-3 leading-relaxed">
                  {String(p.brief.idea ?? '')}
                </div>
                <div className="text-[10.5px] text-[#4f6bff] mt-2.5 opacity-0 group-hover:opacity-100 transition-opacity">
                  开始 →
                </div>
              </button>
            ))}
          </div>
        </section>

        {/* 通道 */}
        <section className="mt-8">
          <h2 className="text-[13.5px] font-medium mb-3">产出方式</h2>
          <div className="grid grid-cols-2 gap-2.5">
            {([
              { id: 'fast' as const, title: '3 分钟草稿', desc: `只跑 ${fastStages.length} 个阶段，先看方向对不对`, badge: '推荐先用这个' },
              { id: 'slow' as const, title: '精修通道', desc: `完整 ${skill.stages.length} 个阶段，含角色设计与配音方案`, badge: '' },
            ]).map((c) => (
              <button
                key={c.id}
                onClick={() => setChannel(c.id)}
                className={`text-left rounded-xl border p-3.5 transition-all ${
                  channel === c.id
                    ? 'border-[#4f6bff]/60 bg-[#4f6bff]/10'
                    : 'border-[#1e2839] bg-[#0f1420] hover:border-[#2a3750]'
                }`}
              >
                <div className="flex items-center gap-2">
                  <span className="text-[12.5px] font-medium">{c.title}</span>
                  {c.badge && <span className="text-[9.5px] px-1.5 py-[2px] rounded bg-[#4f6bff]/20 text-[#8ba3ff]">{c.badge}</span>}
                </div>
                <div className="text-[11px] text-[#6f7d99] mt-1.5 leading-relaxed">{c.desc}</div>
              </button>
            ))}
          </div>
          <div className="text-[11px] text-[#6f7d99] mt-2.5 flex items-center gap-1.5">
            <span className="text-amber-400">◆</span>
            过程中会有 {gates.length} 个确认点（{gates.map((g) => g.name).join('、') || '无'}），其余由模型自行决定
          </div>
        </section>

        {/* 自定义输入 */}
        <section className="mt-8">
          <h2 className="text-[13.5px] font-medium mb-3">
            或者自己描述 <span className="text-[11px] text-[#5f6d87] font-normal">（已填好默认值，可直接改）</span>
          </h2>
          <div className="space-y-3">
            {skill.entry_inputs.map((inp) => (
              <div key={inp.key}>
                <label className="block text-[11.5px] text-[#8b98b4] mb-1.5">{inp.label}</label>
                {inp.type === 'textarea' ? (
                  <textarea
                    rows={2}
                    value={brief[inp.key] ?? ''}
                    placeholder={inp.placeholder}
                    onChange={(e) => setBrief({ ...brief, [inp.key]: e.target.value })}
                    className="w-full rounded-lg bg-[#0f1420] border border-[#1e2839] px-3 py-2 text-[12.5px] text-[#e6ecf7] placeholder-[#4a566e] outline-none focus:border-[#4f6bff]/60 resize-none"
                  />
                ) : inp.type === 'select' ? (
                  <select
                    value={brief[inp.key] ?? ''}
                    onChange={(e) => setBrief({ ...brief, [inp.key]: e.target.value })}
                    className="w-full rounded-lg bg-[#0f1420] border border-[#1e2839] px-3 py-2 text-[12.5px] outline-none focus:border-[#4f6bff]/60"
                  >
                    {(inp.options ?? []).map((o) => <option key={o} value={o}>{o}</option>)}
                  </select>
                ) : (
                  <input
                    type={inp.type === 'number' ? 'number' : 'text'}
                    value={brief[inp.key] ?? ''}
                    placeholder={inp.placeholder}
                    onChange={(e) => setBrief({ ...brief, [inp.key]: e.target.value })}
                    className="w-full rounded-lg bg-[#0f1420] border border-[#1e2839] px-3 py-2 text-[12.5px] outline-none focus:border-[#4f6bff]/60"
                  />
                )}
              </div>
            ))}
          </div>

          <button
            disabled={busy}
            onClick={() => onCreate({ ...brief }, null, channel)}
            className="mt-5 w-full rounded-xl bg-[#4f6bff] hover:bg-[#5f79ff] disabled:opacity-50 text-white text-[13px] font-medium py-3 transition-colors"
          >
            {busy ? '正在创建…' : `开始生成${channel === 'fast' ? ' · 3 分钟草稿' : ' · 精修'}`}
          </button>
        </section>

        {/* 阶段预览 */}
        <section className="mt-9 pb-10">
          <h2 className="text-[13.5px] font-medium mb-3">这条流水线会做什么</h2>
          <div className="rounded-xl border border-[#1e2839] overflow-hidden">
            {skill.stages.map((s, i) => (
              <div
                key={s.id}
                className="flex items-start gap-3 px-3.5 py-3 border-b border-[#151d2b] last:border-0"
                style={{ opacity: channel === 'fast' && !s.fast_channel ? 0.4 : 1 }}
              >
                <div className="w-5 h-5 rounded-md bg-[#1a2233] text-[10.5px] text-[#7c8aa5] flex items-center justify-center shrink-0 mt-[1px]">
                  {i + 1}
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-[12.5px] font-medium">{s.icon} {s.name}</span>
                    {s.human_approval_default && (
                      <span className="text-[9.5px] px-1.5 py-[2px] rounded bg-amber-500/15 text-amber-300">◆ 人工确认</span>
                    )}
                    {channel === 'fast' && !s.fast_channel && (
                      <span className="text-[9.5px] px-1.5 py-[2px] rounded bg-[#1a2233] text-[#5f6d87]">精修通道才跑</span>
                    )}
                  </div>
                  <div className="text-[11px] text-[#6f7d99] mt-1 leading-relaxed">{s.description}</div>
                </div>
              </div>
            ))}
          </div>
        </section>
      </div>
    </div>
  )
}
