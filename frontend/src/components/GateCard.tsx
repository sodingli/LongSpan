import { useState } from 'react'
import type { Gate } from '../lib/types'

interface Props {
  gate: Gate
  busy: boolean
  onResolve: (status: 'approved' | 'rejected' | 'revised', feedback: string) => void
  onOpenFile: (rel: string) => void
}

export default function GateCard({ gate, busy, onResolve, onOpenFile }: Props) {
  const [feedback, setFeedback] = useState('')
  const [showRevise, setShowRevise] = useState(false)

  if (gate.status !== 'pending') {
    return (
      <div className="rounded-xl border border-[#1e2839] bg-[#0f1420] px-4 py-3">
        <div className="flex items-center gap-2 text-[12px]">
          <span className="text-amber-400">◆</span>
          <span className="text-[#8b98b4]">{gate.title}</span>
          <span className={`ml-auto text-[11px] ${
            gate.status === 'approved' ? 'text-emerald-300' : gate.status === 'revised' ? 'text-orange-300' : 'text-rose-300'
          }`}>
            {gate.status === 'approved' ? '已确认' : gate.status === 'revised' ? '已提交修改意见' : '未通过'}
          </span>
        </div>
        {gate.decision?.feedback && (
          <div className="text-[11.5px] text-[#6f7d99] mt-2 leading-relaxed">你的意见：{String(gate.decision.feedback)}</div>
        )}
      </div>
    )
  }

  return (
    <div className="rounded-xl border border-amber-500/40 bg-gradient-to-b from-amber-500/[0.09] to-transparent overflow-hidden fade-slide">
      {/* 头部：强烈的"该你了"信号 */}
      <div className="px-4 py-3 border-b border-amber-500/20 flex items-center gap-2.5">
        <span className="w-6 h-6 rounded-full bg-amber-500/20 text-amber-300 flex items-center justify-center text-[12px] pulse-ring">◆</span>
        <div className="flex-1">
          <div className="text-[13px] font-medium text-amber-100">{gate.title}</div>
          <div className="text-[11px] text-amber-200/60 mt-0.5">方向确认点 · 现在就停在这里等你</div>
        </div>
      </div>

      <div className="px-4 py-3.5">
        <p className="text-[12.5px] text-[#b9c6dc] leading-relaxed">{gate.prompt}</p>

        {/* 待确认产物 */}
        {(gate.proposed?.files?.length ?? 0) > 0 && (
          <div className="mt-3.5">
            <div className="text-[11px] text-[#7c8aa5] mb-1.5">看一下这些产物：</div>
            <div className="flex flex-wrap gap-1.5">
              {(gate.proposed.files as string[]).map((f) => (
                <button
                  key={f}
                  onClick={() => onOpenFile(f)}
                  className="text-[11.5px] px-2.5 py-1 rounded-lg bg-[#151d2b] border border-[#26324a] hover:border-[#4f6bff]/50 hover:text-white text-[#a8b6d1] font-mono transition-colors"
                >
                  {f} ↗
                </button>
              ))}
            </div>
          </div>
        )}

        {/* 自检要点 */}
        {(gate.proposed?.review_focus?.length ?? 0) > 0 && (
          <div className="mt-3.5 rounded-lg bg-[#0d1320] border border-[#1e2839] px-3 py-2.5">
            <div className="text-[11px] text-[#7c8aa5] mb-1.5">建议重点看这几处：</div>
            <ul className="space-y-1">
              {(gate.proposed.review_focus as string[]).map((r, i) => (
                <li key={i} className="text-[11.5px] text-[#8b98b4] flex gap-1.5 leading-relaxed">
                  <span className="text-[#4f6bff] shrink-0">{i + 1}.</span>
                  <span>{r}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {!showRevise ? (
          <div className="flex gap-2.5 mt-4">
            <button
              disabled={busy}
              onClick={() => onResolve('approved', '')}
              className="flex-1 rounded-lg bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-white text-[12.5px] font-medium py-2.5 transition-colors"
            >
              没问题，继续
            </button>
            <button
              disabled={busy}
              onClick={() => setShowRevise(true)}
              className="flex-1 rounded-lg bg-[#1a2233] hover:bg-[#24304a] disabled:opacity-50 text-[#c9d5ea] text-[12.5px] font-medium py-2.5 transition-colors"
            >
              我要改一改
            </button>
          </div>
        ) : (
          <div className="mt-4">
            <textarea
              autoFocus
              rows={3}
              value={feedback}
              onChange={(e) => setFeedback(e.target.value)}
              placeholder="说清楚哪里要改就行，不用写成指令。例如：结尾钩子太弱，把搜索『筷子先生』改成主厨直接念出这个名字"
              className="w-full rounded-lg bg-[#0d1320] border border-[#1e2839] px-3 py-2.5 text-[12.5px] text-[#e6ecf7] placeholder-[#4a566e] outline-none focus:border-[#4f6bff]/60 resize-none leading-relaxed"
            />
            <div className="flex gap-2.5 mt-2.5">
              <button
                disabled={busy || !feedback.trim()}
                onClick={() => onResolve('revised', feedback)}
                className="flex-1 rounded-lg bg-[#4f6bff] hover:bg-[#5f79ff] disabled:opacity-40 text-white text-[12.5px] font-medium py-2.5 transition-colors"
              >
                带上意见重做这一段
              </button>
              <button
                onClick={() => setShowRevise(false)}
                className="px-4 rounded-lg bg-[#1a2233] hover:bg-[#24304a] text-[#c9d5ea] text-[12.5px] py-2.5 transition-colors"
              >
                取消
              </button>
            </div>
            <div className="text-[10.5px] text-[#5f6d87] mt-2">
              只有这一段会重做，前面的产物会保留。
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
