import { useEffect, useRef, useState } from 'react'
import type { RunEvent } from '../lib/types'
import { eventStyle, fmtTime } from '../lib/format'

interface Props {
  events: RunEvent[]
  streamState: 'open' | 'connecting' | 'closed'
}

export default function EventStream({ events, streamState }: Props) {
  const boxRef = useRef<HTMLDivElement>(null)
  const [pinned, setPinned] = useState(true)
  const [filter, setFilter] = useState<'all' | 'story'>('story')

  // 自动吸底：只有用户没往上翻时才自动滚
  useEffect(() => {
    if (pinned && boxRef.current) {
      boxRef.current.scrollTop = boxRef.current.scrollHeight
    }
  }, [events, pinned])

  const onScroll = () => {
    const el = boxRef.current
    if (!el) return
    setPinned(el.scrollHeight - el.scrollTop - el.clientHeight < 60)
  }

  // 「故事线」视图过滤掉噪音（工具调用细节、检查点），留下人看得懂的脉络
  const visible = events.filter((e) => {
    if (filter === 'all') return true
    if (e.type === 'stage.checkpoint') return false
    if (e.type === 'run.progress') return false
    if (e.type === 'agent.thought') return false
    if (e.type === 'run.status') return false
    return true
  })

  return (
    <div className="w-[360px] shrink-0 h-full flex flex-col border-l border-[#1e2839] bg-[#0b0f17]">
      <div className="px-4 py-3 border-b border-[#1e2839] flex items-center gap-2">
        <span className="text-[12.5px] font-medium">实时过程</span>
        <div className="flex items-center gap-0.5 ml-1 rounded-lg bg-[#0f1420] p-0.5">
          {([['story', '脉络'], ['all', '全部']] as const).map(([k, label]) => (
            <button
              key={k}
              onClick={() => setFilter(k)}
              className={`text-[10.5px] px-2 py-[3px] rounded-md transition-colors ${
                filter === k ? 'bg-[#1a2233] text-[#c9d5ea]' : 'text-[#5f6d87] hover:text-[#8b98b4]'
              }`}
            >
              {label}
            </button>
          ))}
        </div>
        <span className="ml-auto flex items-center gap-1.5">
          <span className={`w-1.5 h-1.5 rounded-full ${
            streamState === 'open' ? 'bg-emerald-400' : streamState === 'connecting' ? 'bg-amber-400' : 'bg-slate-600'
          }`} />
          <span className="text-[10px] text-[#5f6d87]">
            {streamState === 'open' ? '已连接' : streamState === 'connecting' ? '重连中' : '未连接'}
          </span>
        </span>
      </div>

      <div ref={boxRef} onScroll={onScroll} className="flex-1 overflow-y-auto px-4 py-3 space-y-2">
        {visible.length === 0 && (
          <div className="text-[11.5px] text-[#5f6d87] leading-relaxed pt-2">
            任务开始后，这里会实时显示它在做什么。
          </div>
        )}

        {visible.map((e) => {
          const st = eventStyle(e.type)
          const isMessage = e.type === 'agent.message'
          const text = isMessage ? (e.payload?.full as string) || e.message : e.message

          return (
            <div key={`${e.id}-${e.at}`} className="flex gap-2 fade-slide">
              <span className={`text-[10px] ${st.color} shrink-0 mt-[3px] w-3 text-center`}>{st.icon}</span>
              <div className="flex-1 min-w-0">
                <div className="flex items-baseline gap-2">
                  <span className="text-[9.5px] text-[#4a566e] font-mono shrink-0">{fmtTime(e.at)}</span>
                  {!isMessage && (
                    <span className={`text-[11.5px] ${st.color} leading-relaxed break-words`}>{e.message}</span>
                  )}
                </div>
                {isMessage && (
                  <div className="text-[11.5px] text-[#b9c6dc] leading-relaxed whitespace-pre-wrap mt-0.5 break-words">
                    {text}
                  </div>
                )}
              </div>
            </div>
          )
        })}

        {!pinned && (
          <button
            onClick={() => setPinned(true)}
            className="sticky bottom-0 w-full text-[11px] py-1.5 rounded-lg bg-[#4f6bff]/20 text-[#8ba3ff] hover:bg-[#4f6bff]/30 transition-colors"
          >
            回到最新 ↓
          </button>
        )}
      </div>
    </div>
  )
}
