import type { Step } from '../lib/types'
import { statusTone } from '../lib/format'

interface Props {
  steps: Step[]
  currentStageId: string | null
  runStatus: string
}

export default function StageTree({ steps, currentStageId, runStatus }: Props) {
  if (!steps.length) {
    return <div className="text-[12px] text-[#5f6d87] px-1 py-4">阶段尚未初始化…</div>
  }

  return (
    <div className="relative">
      {/* 竖线 */}
      <div className="absolute left-[13px] top-3 bottom-3 w-px bg-[#1e2839]" />

      <div className="space-y-1">
        {steps.map((s) => {
          const tone = statusTone(s.status)
          const isCurrent = s.stage_id === currentStageId || (runStatus === 'awaiting_gate' && s.status === 'in_progress')
          const running = s.status === 'in_progress'

          return (
            <div key={s.id} className="relative flex gap-3 pl-0 py-1.5">
              {/* 节点 */}
              <div className="relative z-10 shrink-0 mt-[3px]">
                <div
                  className={`w-[27px] h-[27px] rounded-full border flex items-center justify-center text-[11px] transition-all ${
                    running
                      ? 'border-[#4f6bff] bg-[#4f6bff]/20 text-[#8ba3ff] pulse-ring'
                      : s.status === 'completed'
                      ? 'border-emerald-500/45 bg-emerald-500/12 text-emerald-300'
                      : s.status === 'failed'
                      ? 'border-rose-500/45 bg-rose-500/12 text-rose-300'
                      : isCurrent
                      ? 'border-amber-500/45 bg-amber-500/12 text-amber-300'
                      : 'border-[#26324a] bg-[#0f1420] text-[#5f6d87]'
                  }`}
                >
                  {s.status === 'completed' ? '✓' : s.status === 'failed' ? '✕' : s.index + 1}
                </div>
              </div>

              {/* 内容 */}
              <div className="flex-1 min-w-0 pb-1">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className={`text-[13px] font-medium ${isCurrent ? 'text-white' : tone.text}`}>
                    {s.stage_name}
                  </span>
                  {running && <span className="text-[10.5px] text-[#4f6bff]">进行中…</span>}
                  {s.attempt > 1 && (
                    <span className="text-[9.5px] px-1.5 py-[2px] rounded bg-orange-500/15 text-orange-300">
                      重做 {s.attempt - 1} 次
                    </span>
                  )}
                  {s.detail?.criteria_ok === false && (
                    <span className="text-[9.5px] px-1.5 py-[2px] rounded bg-rose-500/15 text-rose-300">验收未过</span>
                  )}
                </div>

                {(s.detail?.files?.length ?? 0) > 0 && (
                  <div className="flex flex-wrap gap-1.5 mt-1.5">
                    {(s.detail.files as string[]).map((f: string) => (
                      <span key={f} className="text-[10.5px] px-1.5 py-[2px] rounded bg-[#151d2b] text-[#8b98b4] font-mono">
                        {f}
                      </span>
                    ))}
                  </div>
                )}

                {s.summary && (
                  <div className="text-[11.5px] text-[#6f7d99] mt-1.5 leading-relaxed line-clamp-2">{s.summary}</div>
                )}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
