import type { RunStatus } from './types'

export const STATUS_TEXT: Record<RunStatus | string, string> = {
  pending: '待启动',
  running: '执行中',
  awaiting_gate: '等你确认',
  paused: '已暂停',
  succeeded: '已完成',
  failed: '失败',
  cancelled: '已取消',
  interrupted: '已中断',
}

export function statusTone(s: string): { text: string; bg: string; dot: string } {
  switch (s) {
    case 'running':
    case 'in_progress':
      return { text: 'text-blue-300', bg: 'bg-blue-500/12 border-blue-500/30', dot: 'bg-blue-400' }
    case 'completed':
    case 'succeeded':
      return { text: 'text-emerald-300', bg: 'bg-emerald-500/12 border-emerald-500/30', dot: 'bg-emerald-400' }
    case 'awaiting_gate':
      return { text: 'text-amber-300', bg: 'bg-amber-500/12 border-amber-500/35', dot: 'bg-amber-400' }
    case 'failed':
      return { text: 'text-rose-300', bg: 'bg-rose-500/12 border-rose-500/30', dot: 'bg-rose-400' }
    case 'interrupted':
      // 不是失败：产物都在，只是执行被打断，一步就能续上
      return { text: 'text-orange-300', bg: 'bg-orange-500/12 border-orange-500/35', dot: 'bg-orange-400' }
    default:
      return { text: 'text-slate-400', bg: 'bg-slate-500/10 border-slate-600/30', dot: 'bg-slate-500' }
  }
}

export function fmtSize(n: number): string {
  if (!n) return '0 B'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

export function fmtTime(iso: string | null): string {
  if (!iso) return '-'
  const d = new Date(iso.endsWith('Z') || iso.includes('+') ? iso : iso + 'Z')
  return d.toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

export function fmtAgo(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso.endsWith('Z') || iso.includes('+') ? iso : iso + 'Z')
  const diff = (Date.now() - d.getTime()) / 1000
  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  return `${Math.floor(diff / 86400)} 天前`
}

/** 秒 → 人类可读（北极星指标的展示形态：让"用了多久"一眼可比） */
export function fmtDuration(sec: number | null): string {
  if (sec == null) return '—'
  if (sec < 1) return `${sec.toFixed(1)} 秒`
  if (sec < 60) return `${Math.round(sec)} 秒`
  const m = Math.floor(sec / 60)
  const s = Math.round(sec - m * 60)
  return s ? `${m} 分 ${s} 秒` : `${m} 分钟`
}

export function fmtClock(sec: number): string {
  if (sec < 60) return `${sec} 秒`
  return `${Math.round(sec / 60)} 分钟`
}

/** 事件类型 → 展示用图标与配色 */
export function eventStyle(type: string): { icon: string; color: string } {
  if (type.startsWith('gate')) return { icon: '◆', color: 'text-amber-300' }
  if (type.startsWith('draft.ready')) return { icon: '◇', color: 'text-teal-300' }
  if (type.startsWith('run.resumed')) return { icon: '↻', color: 'text-violet-300' }
  if (type.startsWith('run.finished')) return { icon: '●', color: 'text-emerald-300' }
  if (type.startsWith('run.error')) return { icon: '●', color: 'text-rose-300' }
  if (type.startsWith('stage.completed')) return { icon: '✓', color: 'text-emerald-300' }
  if (type.startsWith('stage.revision')) return { icon: '↻', color: 'text-orange-300' }
  if (type.startsWith('stage')) return { icon: '▸', color: 'text-blue-300' }
  if (type.startsWith('artifact')) return { icon: '▤', color: 'text-violet-300' }
  if (type.startsWith('decision')) return { icon: '⚖', color: 'text-cyan-300' }
  if (type.startsWith('tool')) return { icon: '⚙', color: 'text-slate-400' }
  if (type.startsWith('agent.message')) return { icon: '💬', color: 'text-slate-200' }
  if (type.startsWith('agent.thought')) return { icon: '…', color: 'text-slate-500' }
  if (type.startsWith('quota')) return { icon: '▲', color: 'text-orange-300' }
  return { icon: '·', color: 'text-slate-400' }
}
