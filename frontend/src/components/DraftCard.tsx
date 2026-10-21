import type { DraftInfo } from '../lib/types'
import { fmtDuration } from '../lib/format'

interface Props {
  draft: DraftInfo
  busy: boolean
  onUpgrade: () => void
}

/**
 * 草稿卡片 —— 这个产品的诚实性开关。
 *
 * 设计立场（来自决策 D9）：用户「试一下就出效果」的前提是**他知道自己拿到的是什么**。
 * 所以我们不做两件事：不假装草稿是成品，不把差距藏进免责声明。
 * 三件事必须一眼可见：用了多久、差在哪、下一步怎么变好。
 */
export default function DraftCard({ draft, busy, onUpgrade }: Props) {
  const { is_draft, ttfv_seconds, target_seconds, within_target, gaps, unrun_stages, can_upgrade } = draft

  // 精修通道：只留一条很轻的说明，不抢视线
  if (!is_draft) {
    return (
      <div className="mt-6 rounded-xl border border-[#1e2839] bg-[#0f1420] px-4 py-3 flex items-center gap-2.5 text-[11.5px]">
        <span className="text-violet-300">↻</span>
        <span className="text-[#8b98b4]">
          {draft.resume_count > 0
            ? `已在草稿基础上续跑 ${draft.resume_count} 次 · 同为一份产物，未重新开始`
            : '精修通道 · 完整流水线'}
        </span>
        {ttfv_seconds != null && (
          <span className="text-[#5f6d87] ml-auto shrink-0">
            首个成品用时 {fmtDuration(ttfv_seconds)}
          </span>
        )}
      </div>
    )
  }

  return (
    <div className="mt-6 rounded-xl border border-amber-500/25 bg-amber-500/[0.045] overflow-hidden">
      {/* 头部：用时 vs 目标 */}
      <div className="px-4 py-3.5 flex items-start gap-3 border-b border-amber-500/15">
        <span className="text-[15px] leading-none mt-[1px] text-amber-300">◇</span>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-[13px] font-medium text-amber-100">这是一份草稿</span>
            {within_target === true && (
              <span className="text-[9.5px] px-1.5 py-[2px] rounded bg-emerald-500/15 text-emerald-300">
                在 {Math.round(target_seconds / 60)} 分钟目标内
              </span>
            )}
            {within_target === false && (
              <span className="text-[9.5px] px-1.5 py-[2px] rounded bg-rose-500/15 text-rose-300">
                超出 {Math.round(target_seconds / 60)} 分钟目标
              </span>
            )}
          </div>
          <div className="text-[11.5px] text-amber-200/70 mt-1.5 leading-relaxed">
            首次成品用时 <span className="text-amber-100 font-medium">{fmtDuration(ttfv_seconds)}</span>
            <span className="text-amber-200/40"> · 目标 {Math.round(target_seconds / 60)} 分钟 · </span>
            先看方向对不对，再决定要不要接着做。
          </div>
        </div>
      </div>

      {/* 还能怎么变好 */}
      {(gaps.length > 0 || unrun_stages.length > 0) && (
        <div className="px-4 py-3.5">
          <div className="text-[11.5px] font-medium text-amber-100/90 mb-2.5">还能怎么变好</div>
          <ul className="space-y-1.5">
            {gaps.map((g, i) => (
              <li key={i} className="flex gap-2 text-[11.5px] text-[#a8b4cc] leading-relaxed">
                <span className="text-amber-300/60 shrink-0 mt-[1px]">✧</span>
                <span>{g}</span>
              </li>
            ))}
          </ul>

          {unrun_stages.length > 0 && (
            <div className="mt-3 pt-3 border-t border-amber-500/10 flex items-center gap-2 flex-wrap">
              <span className="text-[11px] text-[#6f7d99]">还没跑：</span>
              {unrun_stages.map((s) => (
                <span
                  key={s.id}
                  className="text-[11px] px-2 py-[3px] rounded-md bg-[#1a2233] text-[#8b98b4]"
                  title={s.reason}
                >
                  {s.icon} {s.name}
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      {/* 继续优化 */}
      {can_upgrade && (
        <div className="px-4 py-3 bg-amber-500/[0.05] border-t border-amber-500/15 flex items-center gap-3">
          <button
            disabled={busy}
            onClick={onUpgrade}
            className="rounded-lg bg-amber-500/90 hover:bg-amber-400 disabled:opacity-50 text-[#1a1200] text-[12px] font-medium px-3.5 py-2 transition-colors shrink-0"
          >
            {busy ? '正在续跑…' : '继续优化'}
          </button>
          <span className="text-[11px] text-amber-200/60 leading-relaxed">
            在<span className="text-amber-200/90 font-medium">同一份</span>产物上接着深化，已完成的阶段不重跑。
          </span>
        </div>
      )}
    </div>
  )
}
