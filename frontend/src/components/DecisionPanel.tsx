import { useState } from 'react'
import type { Decision } from '../lib/types'

interface Props {
  decisions: Decision[]
}

/**
 * 决策留痕面板 —— 对应 MRD §7.3.4。
 * 用户只提约束，但不能失去控制：模型每个自主决定在这里可见、可追溯。
 */
export default function DecisionPanel({ decisions }: Props) {
  const [expanded, setExpanded] = useState<string | null>(null)
  const modelDecisions = decisions.filter((d) => d.actor === 'model')

  return (
    <section>
      <div className="flex items-baseline gap-2 mb-3">
        <h2 className="text-[13.5px] font-medium">模型替你做的决定 · {modelDecisions.length}</h2>
        <span className="text-[10.5px] text-[#5f6d87]">你没写步骤，但这些判断都摆在这里</span>
      </div>

      {modelDecisions.length === 0 && (
        <div className="text-[11.5px] text-[#5f6d87] px-1 py-3">还没有自主决策记录。</div>
      )}

      <div className="space-y-2">
        {modelDecisions.map((d) => {
          const open = expanded === d.id
          return (
            <div key={d.id} className="rounded-xl border border-[#1e2839] bg-[#0f1420] overflow-hidden">
              <button
                onClick={() => setExpanded(open ? null : d.id)}
                className="w-full text-left px-3.5 py-2.5 hover:bg-[#121826] transition-colors"
              >
                <div className="flex items-start gap-2">
                  <span className="text-[11px] text-cyan-400 mt-[2px] shrink-0">⚖</span>
                  <span className="text-[12.5px] text-[#d5dfef] leading-snug flex-1">{d.what}</span>
                  {d.alternatives?.length > 0 && (
                    <span className="text-[10px] text-[#5f6d87] shrink-0 mt-[3px]">{open ? '收起' : '看备选'}</span>
                  )}
                </div>
              </button>

              {open && (
                <div className="px-3.5 pb-3 pt-1 border-t border-[#151d2b]">
                  <div className="text-[11px] text-[#7c8aa5] mt-2 mb-1">依据</div>
                  <p className="text-[11.5px] text-[#8b98b4] leading-relaxed">{d.why}</p>

                  {d.alternatives?.length > 0 && (
                    <>
                      <div className="text-[11px] text-[#7c8aa5] mt-3 mb-1.5">被放弃的选项</div>
                      <div className="space-y-1.5">
                        {d.alternatives.map((a, i) => (
                          <div key={i} className="rounded-lg bg-[#0d1320] border border-[#1e2839] px-2.5 py-2">
                            <div className="flex items-center gap-2">
                              <span className="text-[11.5px] text-[#8b98b4]">{a.option}</span>
                              {typeof a.confidence === 'number' && (
                                <span className="ml-auto flex items-center gap-1.5">
                                  <span className="w-14 h-1 rounded bg-[#1e2839] overflow-hidden">
                                    <span
                                      className="block h-full bg-[#4f6bff]"
                                      style={{ width: `${Math.round(a.confidence * 100)}%` }}
                                    />
                                  </span>
                                  <span className="text-[10px] text-[#5f6d87]">{Math.round(a.confidence * 100)}%</span>
                                </span>
                              )}
                            </div>
                            {a.reason && <div className="text-[10.5px] text-[#6f7d99] mt-1 leading-relaxed">{a.reason}</div>}
                          </div>
                        ))}
                      </div>
                    </>
                  )}

                  {d.reversible && (
                    <div className="text-[10.5px] text-[#5f6d87] mt-2.5">这个决定可以推翻——下次重跑这一段时告诉它就行。</div>
                  )}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </section>
  )
}
