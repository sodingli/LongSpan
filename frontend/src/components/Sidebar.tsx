import type { RunSummary, SkillSummary } from '../lib/types'
import { STATUS_TEXT, fmtAgo, statusTone } from '../lib/format'

interface Props {
  skills: SkillSummary[]
  runs: RunSummary[]
  currentRunId: string | null
  activeSkillId: string | null
  meta: { llm_mode: string; model: string } | null
  onSelectRun: (id: string) => void
  onPickSkill: (id: string) => void
  onNewTask: () => void
  onOpenModelConfig: () => void
  onOpenCast: () => void
  castActive: boolean
}

export default function Sidebar({
  skills, runs, currentRunId, activeSkillId, meta, onSelectRun, onPickSkill, onNewTask, onOpenModelConfig,
  onOpenCast, castActive,
}: Props) {
  return (
    <aside className="w-[276px] shrink-0 h-full flex flex-col border-r border-[#1e2839] bg-[#0b0f17]">
      {/* 品牌 */}
      <div className="px-4 py-4 border-b border-[#1e2839]">
        <div className="flex items-center gap-2.5">
          <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-[#4f6bff] to-[#8b5cf6] flex items-center justify-center text-[15px] font-bold">
            L
          </div>
          <div>
            <div className="text-[15px] font-semibold tracking-tight leading-none">Longspan</div>
            <div className="text-[10.5px] text-[#7c8aa5] mt-[3px]">长任务工坊 · 长跑</div>
          </div>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto">
        {/* 资产（工作台级，不属于任何一次任务） */}
        <div className="px-3 pt-3">
          <button
            onClick={onOpenCast}
            className={`w-full flex items-center gap-2 rounded-lg px-2.5 py-2 border transition-all ${
              castActive
                ? 'border-[#4f6bff]/50 bg-[#4f6bff]/10'
                : 'border-[#1e2839] bg-[#0f1420] hover:border-[#2a3750] hover:bg-[#121826]'
            }`}
          >
            <span className="w-4 h-4 rounded shrink-0 bg-gradient-to-br from-[#4f6bff] to-[#8b5cf6] opacity-80" />
            <span className="text-[12.5px] font-medium">角色库</span>
            <span className="ml-auto text-[10px] text-[#5f6d87]">跨集复用</span>
          </button>
        </div>

        {/* 技能 */}
        <div className="px-3 pt-4">
          <div className="flex items-center justify-between px-1 mb-2">
            <span className="text-[10.5px] font-medium text-[#7c8aa5] tracking-wider uppercase">技能</span>
            <button
              onClick={onNewTask}
              className="text-[11px] text-[#4f6bff] hover:text-[#8ba3ff] transition-colors"
            >
              + 新任务
            </button>
          </div>
          <div className="space-y-1.5">
            {skills.map((s) => (
              <button
                key={s.id}
                onClick={() => onPickSkill(s.id)}
                className={`w-full text-left rounded-lg px-2.5 py-2 border transition-all ${
                  activeSkillId === s.id
                    ? 'border-[#4f6bff]/50 bg-[#4f6bff]/10'
                    : 'border-[#1e2839] bg-[#0f1420] hover:border-[#2a3750] hover:bg-[#121826]'
                }`}
              >
                <div className="flex items-center gap-2">
                  <span className="text-[15px]">{s.icon}</span>
                  <span className="text-[12.5px] font-medium truncate">{s.name}</span>
                  <span className="ml-auto text-[10px] text-[#5f6d87]">{s.stage_count} 阶段</span>
                </div>
                <div className="text-[10.5px] text-[#7c8aa5] mt-1 line-clamp-2 leading-snug">{s.description}</div>
              </button>
            ))}
          </div>
        </div>

        {/* 任务 */}
        <div className="px-3 pt-5 pb-4">
          <div className="px-1 mb-2 text-[10.5px] font-medium text-[#7c8aa5] tracking-wider uppercase">
            任务 · {runs.length}
          </div>
          {runs.length === 0 && (
            <div className="px-2.5 py-3 text-[11.5px] text-[#5f6d87] leading-relaxed">
              还没有任务。点上面的技能，或直接用一个示例跑起来。
            </div>
          )}
          <div className="space-y-1">
            {runs.map((r) => {
              const tone = statusTone(r.status)
              return (
                <button
                  key={r.id}
                  onClick={() => onSelectRun(r.id)}
                  className={`w-full text-left rounded-lg px-2.5 py-2 border transition-all ${
                    currentRunId === r.id
                      ? 'border-[#4f6bff]/50 bg-[#4f6bff]/10'
                      : 'border-transparent hover:border-[#1e2839] hover:bg-[#0f1420]'
                  }`}
                >
                  <div className="flex items-center gap-1.5">
                    <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${tone.dot} ${r.status === 'running' ? 'pulse-ring' : ''}`} />
                    <span className="text-[12px] truncate flex-1">{r.title || '未命名任务'}</span>
                  </div>
                  <div className="flex items-center gap-2 mt-1 pl-3">
                    <span className={`text-[10px] ${tone.text}`}>{STATUS_TEXT[r.status]}</span>
                    <span className="text-[10px] text-[#5f6d87]">{r.channel === 'fast' ? '草稿' : '精修'}</span>
                    <span className="text-[10px] text-[#5f6d87] ml-auto">{fmtAgo(r.created_at)}</span>
                  </div>
                </button>
              )
            })}
          </div>
        </div>
      </div>

      {/* 底部：模型状态 + 配置入口 */}
      <button
        onClick={onOpenModelConfig}
        title="配置模型与接口"
        className="w-full px-4 py-3 border-t border-[#1e2839] text-left hover:bg-[#0f1420] transition-colors group"
      >
        <div className="flex items-center gap-1.5">
          <span
            className={`w-1.5 h-1.5 rounded-full shrink-0 ${
              meta?.llm_mode === 'real' ? 'bg-emerald-400' : 'bg-amber-400'
            }`}
          />
          <span className="flex-1 truncate text-[10.5px] text-[#7c8aa5] group-hover:text-[#a8b6d1] transition-colors">
            {meta?.llm_mode === 'real' ? `模型：${meta.model}` : '演示模式（未配置模型）'}
          </span>
          <span className="text-[10px] text-[#4f6bff] group-hover:text-[#8ba3ff] transition-colors shrink-0">
            配置
          </span>
        </div>
      </button>
    </aside>
  )
}
