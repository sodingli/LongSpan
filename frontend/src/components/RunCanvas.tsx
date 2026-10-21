import { Fragment, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import type { Artifact, Decision, Gate, RunEvent, StageAction, Step } from '../lib/types'
import { fmtSize } from '../lib/format'
import CanvasEditor from './CanvasEditor'
import DirectoryTable from './DirectoryTable'

/**
 * 编排画布：把一次 Run 的阶段流水线画出来。
 *
 * 三条设计原则（与项目铁律一致）：
 *  1. **不硬编码阶段名** —— 画布只认 steps/gates/events 的结构，
 *     换一个技能包（ppt-deck）画布自动变成那套阶段，前端一行不用改。
 *  2. **不静默降级** —— 验收未过、重做、模型回退、工具降级全部画在节点上，
 *     谁也别想悄悄藏起来。
 *  3. **耗时不估算** —— 时间轴只用 Step.started_at/finished_at 的实数，
 *     拿不到就如实显示"—"，不拿估算值糊弄。
 */

const NODE_W = 176
const GAP_W = 36

interface Props {
  runId: string
  steps: Step[]
  gates: Gate[]
  events: RunEvent[]
  artifacts: Artifact[]
  decisions: Decision[]
  currentStageId: string | null
  runStatus: string
  busy?: boolean
  onResolveGate: (gateId: string, status: 'approved' | 'rejected' | 'revised', feedback: string) => void
  onOpenFile: (path: string) => void
  onRunChanged?: () => void
}

type NodeState = 'pending' | 'running' | 'waiting' | 'completed' | 'failed' | 'skipped'

const TONE: Record<NodeState, { card: string; bar: string; text: string; chip: string; label: string }> = {
  pending: {
    card: 'border-[#26324a] bg-[#0f1420]',
    bar: 'bg-[#2b3853]',
    text: 'text-[#6f7d99]',
    chip: 'text-[#6f7d99]',
    label: '待跑',
  },
  running: {
    card: 'border-[#4f6bff] bg-[#4f6bff]/10 pulse-ring',
    bar: 'bg-[#4f6bff]',
    text: 'text-[#93a8ff]',
    chip: 'text-[#93a8ff]',
    label: '进行中',
  },
  waiting: {
    card: 'border-amber-500/55 bg-amber-500/10',
    bar: 'bg-amber-400',
    text: 'text-amber-300',
    chip: 'text-amber-300',
    label: '等你确认',
  },
  completed: {
    card: 'border-emerald-500/40 bg-emerald-500/[0.07]',
    bar: 'bg-emerald-500/75',
    text: 'text-emerald-300',
    chip: 'text-emerald-300',
    label: '已完成',
  },
  failed: {
    card: 'border-rose-500/45 bg-rose-500/10',
    bar: 'bg-rose-500',
    text: 'text-rose-300',
    chip: 'text-rose-300',
    label: '失败',
  },
  skipped: {
    card: 'border-[#26324a] bg-[#0f1420]',
    bar: 'bg-[#2b3853]',
    text: 'text-[#6f7d99]',
    chip: 'text-[#6f7d99]',
    label: '已跳过',
  },
}

const TOOL_LABEL: Record<string, string> = {
  generate_images: '生图',
  synthesize_lines: '配音',
  render_video: '渲染',
  placeholder_image: '占位图',
}

const GATE_STATUS: Record<string, string> = {
  pending: '待确认',
  approved: '已通过',
  rejected: '已打回',
  revised: '要求返工',
}

// ------------------------------------------------------------------ 工具函数

function parseTs(iso: string | null | undefined): number | null {
  if (!iso) return null
  const s = iso.endsWith('Z') || iso.includes('+') ? iso : iso + 'Z'
  const t = new Date(s).getTime()
  return Number.isNaN(t) ? null : t
}

function stageSeconds(s: Step): number | null {
  const a = parseTs(s.started_at)
  const b = parseTs(s.finished_at)
  if (a == null || b == null || b < a) return null
  return (b - a) / 1000
}

function fmtSec(sec: number | null): string {
  if (sec == null) return '—'
  if (sec < 60) return `${sec.toFixed(1)}s`
  const m = Math.floor(sec / 60)
  const r = Math.round(sec - m * 60)
  return r ? `${m}m${String(r).padStart(2, '0')}s` : `${m}m`
}

interface ViewNode {
  step: Step
  stageId: string
  name: string
  index: number
  state: NodeState
  seconds: number | null
  attempt: number
  criteriaOk: boolean | null
  failures: string[]
  files: string[]
  actions: StageAction[]
  gate: Gate | null
  fallback: { from: string; to: string; reason: string } | null
  notes: string[]
  decisionCount: number
  artifactBytes: number
}

export default function RunCanvas({
  runId,
  steps,
  gates,
  events,
  artifacts,
  decisions,
  currentStageId,
  runStatus,
  busy,
  onResolveGate,
  onOpenFile,
  onRunChanged,
}: Props) {
  const [selected, setSelected] = useState<string | null>(null)

  const nodes = useMemo<ViewNode[]>(() => {
    // 回退事件（模型不可用自动顶替）—— 结构来自 agent.py 的 payload
    const fallbackByStage: Record<string, { from: string; to: string; reason: string }> = {}
    const notesByStage: Record<string, string[]> = {}
    for (const e of events) {
      const p = (e.payload || {}) as Record<string, any>
      const sid = e.stage_id || ''
      if (p.fallback_from) {
        fallbackByStage[sid] = {
          from: String(p.fallback_from),
          to: String(p.fallback_to || ''),
          reason: String(p.reason || ''),
        }
      }
      if (e.type === 'agent.thought' && typeof e.message === 'string' && e.message.startsWith('降级说明：')) {
        ;(notesByStage[sid] ||= []).push(e.message.replace(/^降级说明：/, ''))
      }
    }

    const gateByStage: Record<string, Gate> = {}
    for (const g of gates) if (g.stage_id) gateByStage[g.stage_id] = g

    const bytesByStage: Record<string, number> = {}
    for (const a of artifacts) {
      if (a.stage_id) bytesByStage[a.stage_id] = (bytesByStage[a.stage_id] || 0) + (a.size || 0)
    }

    const decisionCount: Record<string, number> = {}
    for (const d of decisions) if (d.stage_id) decisionCount[d.stage_id] = (decisionCount[d.stage_id] || 0) + 1

    return steps
      .filter((s) => !s.is_sub_stage)
      .map((s) => {
        const gate = gateByStage[s.stage_id] ?? null
        const d = (s.detail || {}) as Record<string, any>

        let state: NodeState
        if (s.status === 'completed') state = 'completed'
        else if (s.status === 'failed') state = 'failed'
        else if (s.status === 'skipped') state = 'skipped'
        else if (s.status === 'in_progress') state = runStatus === 'awaiting_gate' ? 'waiting' : 'running'
        else state = 'pending'

        const files = Array.isArray(d.files) ? (d.files as string[]) : []
        return {
          step: s,
          stageId: s.stage_id,
          name: s.stage_name || s.stage_id,
          index: s.index,
          state,
          seconds: stageSeconds(s),
          attempt: s.attempt || 0,
          criteriaOk: typeof d.criteria_ok === 'boolean' ? d.criteria_ok : null,
          failures: Array.isArray(d.failures) ? (d.failures as string[]) : [],
          files,
          actions: Array.isArray(d.actions) ? (d.actions as StageAction[]) : [],
          gate,
          fallback: fallbackByStage[s.stage_id] ?? null,
          notes: notesByStage[s.stage_id] ?? [],
          decisionCount: decisionCount[s.stage_id] || 0,
          artifactBytes: bytesByStage[s.stage_id] || 0,
        }
      })
  }, [steps, gates, events, artifacts, decisions, runStatus])

  // 默认选中：正在执行 / 正在等确认的那一格，否则选最后一格
  useEffect(() => {
    if (selected && nodes.some((n) => n.stageId === selected)) return
    const live = nodes.find((n) => n.state === 'running' || n.state === 'waiting')
    if (live) setSelected(live.stageId)
    else if (nodes.length) setSelected(nodes[nodes.length - 1].stageId)
  }, [nodes, selected])

  const totalSeconds = nodes.reduce((acc, n) => acc + (n.seconds || 0), 0)
  const timed = nodes.filter((n) => n.seconds != null).length
  const selectedNode = nodes.find((n) => n.stageId === selected) ?? null
  const artifactIndex = useMemo(() => {
    const m: Record<string, Artifact> = {}
    for (const a of artifacts) m[a.rel_path] = a
    return m
  }, [artifacts])

  if (!nodes.length) {
    return <div className="text-[12px] text-[#5f6d87] px-1 py-4">阶段尚未初始化…</div>
  }

  return (
    <div className="rounded-xl border border-[#1e2839] bg-[#0b0f17]">
      {/* ---------- 画布 ---------- */}
      <div className="overflow-x-auto">
        <div className="flex items-stretch min-w-max px-4 pt-4 pb-3">
          {nodes.map((n, i) => (
            <Fragment key={n.stageId}>
              {i > 0 && <Connector from={nodes[i - 1].state} />}
              <NodeCard node={n} active={n.stageId === selected} onClick={() => setSelected(n.stageId)} />
            </Fragment>
          ))}
        </div>
      </div>

      {/* ---------- 时间轴（真实耗时） ---------- */}
      <div className="px-4 pb-3">
        <div className="flex items-center justify-between text-[10.5px] text-[#5f6d87] mb-1.5">
          <span>
            阶段耗时（实数
            {timed < nodes.length ? `，${nodes.length - timed} 段未记录` : ''}）
          </span>
          <span className="font-mono">{totalSeconds > 0 ? `合计 ${fmtSec(totalSeconds)}` : '无耗时数据'}</span>
        </div>
        <div className="flex h-1.5 rounded-full overflow-hidden bg-[#121826]">
          {totalSeconds > 0 &&
            nodes.map((n) => {
              if (n.seconds == null) return null
              const pct = Math.max((n.seconds / totalSeconds) * 100, 1.2)
              return (
                <div
                  key={n.stageId}
                  className={`h-full ${TONE[n.state].bar} ${n.stageId === selected ? 'brightness-125' : ''}`}
                  style={{ width: `${pct}%` }}
                  title={`${n.name} · ${fmtSec(n.seconds)}`}
                />
              )
            })}
        </div>
      </div>

      {/* ---------- 节点详情 ---------- */}
      {selectedNode && (
        <DetailPanel
          node={selectedNode}
          runId={runId}
          artifactIndex={artifactIndex}
          busy={busy}
          onResolveGate={onResolveGate}
          onOpenFile={onOpenFile}
          onRunChanged={onRunChanged}
          onClose={() => setSelected(null)}
        />
      )}
    </div>
  )
}

// ------------------------------------------------------------------ 连线

function Connector({ from }: { from: NodeState }) {
  const done = from === 'completed'
  const live = from === 'running'
  const bad = from === 'failed'
  const line = bad ? 'bg-rose-500/70' : done ? 'bg-emerald-500/50' : live ? 'bg-[#4f6bff]' : 'bg-[#243049]'
  return (
    <div className="flex items-center shrink-0" style={{ width: GAP_W }}>
      <div className={`h-[2px] w-full rounded-full ${line} ${live ? 'flow-line' : ''}`} />
    </div>
  )
}

// ------------------------------------------------------------------ 节点

function NodeCard({ node, active, onClick }: { node: ViewNode; active: boolean; onClick: () => void }) {
  const tone = TONE[node.state]
  const badges: { text: string; cls: string }[] = []

  if (node.gate) {
    const pending = node.gate.status === 'pending'
    badges.push({
      text: pending ? '◆ 关卡' : `◆ ${GATE_STATUS[node.gate.status] ?? node.gate.status}`,
      cls: pending ? 'bg-amber-500/15 text-amber-300' : 'bg-[#1a2233] text-[#8b98b4]',
    })
  }
  if (node.attempt > 1) badges.push({ text: `重做 ${node.attempt - 1}`, cls: 'bg-orange-500/15 text-orange-300' })
  if (node.criteriaOk === false) badges.push({ text: '验收未过', cls: 'bg-rose-500/15 text-rose-300' })
  if (node.fallback) badges.push({ text: '模型回退', cls: 'bg-violet-500/15 text-violet-300' })
  if (node.notes.length) badges.push({ text: '有降级', cls: 'bg-amber-500/15 text-amber-300' })
  for (const a of node.actions) {
    const label = TOOL_LABEL[a.tool] ?? a.tool
    badges.push({
      text: a.ok === false ? `${label} ✕` : label,
      cls: a.ok === false ? 'bg-rose-500/15 text-rose-300' : 'bg-[#182032] text-[#8b98b4]',
    })
  }

  return (
    <button
      type="button"
      onClick={onClick}
      style={{ width: NODE_W }}
      className={`shrink-0 text-left rounded-xl border px-3 py-2.5 transition-all ${tone.card} ${
        active ? 'ring-1 ring-[#4f6bff]/70' : 'hover:border-[#3a4a6b]'
      }`}
    >
      <div className="flex items-center gap-1.5">
        <span
          className={`w-[17px] h-[17px] rounded-full shrink-0 flex items-center justify-center text-[9.5px] font-medium border ${
            node.state === 'completed'
              ? 'border-emerald-500/50 text-emerald-300'
              : node.state === 'failed'
                ? 'border-rose-500/50 text-rose-300'
                : node.state === 'running' || node.state === 'waiting'
                  ? 'border-[#4f6bff]/60 text-[#93a8ff]'
                  : 'border-[#2b3853] text-[#5f6d87]'
          }`}
        >
          {node.state === 'completed' ? '✓' : node.state === 'failed' ? '✕' : node.index + 1}
        </span>
        <span className={`text-[12.5px] font-medium truncate ${node.state === 'completed' ? 'text-[#cbd6ea]' : tone.text}`}>
          {node.name}
        </span>
      </div>

      <div className="mt-1.5 flex items-center gap-2 text-[10.5px]">
        <span className={tone.chip}>{tone.label}</span>
        <span className="font-mono text-[#6f7d99]">{fmtSec(node.seconds)}</span>
      </div>

      <div className="mt-1.5 flex flex-wrap gap-1 min-h-[17px]">
        {badges.map((b, i) => (
          <span key={i} className={`text-[9.5px] px-1.5 py-[2px] rounded ${b.cls}`}>
            {b.text}
          </span>
        ))}
      </div>
    </button>
  )
}

// ------------------------------------------------------------------ 详情

function DetailPanel({
  node,
  runId,
  artifactIndex,
  busy,
  onResolveGate,
  onOpenFile,
  onRunChanged,
  onClose,
}: {
  node: ViewNode
  runId: string
  artifactIndex: Record<string, Artifact>
  busy?: boolean
  onResolveGate: (gateId: string, status: 'approved' | 'rejected' | 'revised', feedback: string) => void
  onOpenFile: (path: string) => void
  onRunChanged?: () => void
  onClose: () => void
}) {
  const [feedback, setFeedback] = useState('')
  const [editing, setEditing] = useState(false)
  const tone = TONE[node.state]
  const gate = node.gate

  useEffect(() => {
    setFeedback('')
    setEditing(false)
  }, [node.stageId])

  // 本阶段是否已经有可编辑的内容 —— 没有就不展示编辑入口，避免点了空转
  const editable =
    node.files.includes('panels.json') ||
    node.files.includes('characters.md') ||
    node.state === 'completed' ||
    node.decisionCount > 0

  return (
    <div className="border-t border-[#1e2839] px-4 py-3.5 bg-[#0a0e16]">
      <div className="flex items-center gap-2.5 flex-wrap">
        <span className="text-[13px] font-medium text-white">
          {node.index + 1}. {node.name}
        </span>
        <span className={`text-[10px] px-1.5 py-[2px] rounded bg-[#151d2b] ${tone.chip}`}>{tone.label}</span>
        <span className="text-[11px] text-[#6f7d99] font-mono">耗时 {fmtSec(node.seconds)}</span>
        {node.attempt > 1 && (
          <span className="text-[10px] px-1.5 py-[2px] rounded bg-orange-500/15 text-orange-300">
            重做 {node.attempt - 1} 次
          </span>
        )}
        {node.decisionCount > 0 && (
          <span className="text-[10px] px-1.5 py-[2px] rounded bg-cyan-500/12 text-cyan-300">
            决策 {node.decisionCount}
          </span>
        )}
        {node.artifactBytes > 0 && (
          <span className="text-[11px] text-[#5f6d87]">产物 {fmtSize(node.artifactBytes)}</span>
        )}
        {editable && (
          <button
            onClick={() => setEditing((v) => !v)}
            className={`text-[11px] px-2.5 py-1 rounded-lg transition-colors ${
              editing ? 'bg-[#4f6bff]/25 text-[#a9baff]' : 'bg-[#1a2233] hover:bg-[#24304a] text-[#c9d5ea]'
            }`}
          >
            {editing ? '收起编辑' : '编辑本阶段'}
          </button>
        )}
        <button onClick={onClose} className="ml-auto text-[11px] text-[#5f6d87] hover:text-[#8b98b4]">
          收起
        </button>
      </div>

      {node.step.summary && (
        <p className="mt-2 text-[11.5px] leading-relaxed text-[#8b98b4] whitespace-pre-wrap line-clamp-4">
          {node.step.summary}
        </p>
      )}

      {node.fallback && (
        <div className="mt-2.5 rounded-lg border border-violet-500/35 bg-violet-500/10 px-3 py-2">
          <div className="text-[11.5px] text-violet-200">
            主模型 <span className="font-mono">{node.fallback.from}</span> 不可用，已自动回退到{' '}
            <span className="font-mono">{node.fallback.to}</span>
          </div>
          {node.fallback.reason && (
            <div className="text-[10.5px] text-violet-300/70 mt-0.5 break-all">{node.fallback.reason}</div>
          )}
        </div>
      )}

      {node.notes.map((t, i) => (
        <div key={i} className="mt-2.5 rounded-lg border border-amber-500/35 bg-amber-500/10 px-3 py-2 text-[11.5px] text-amber-200">
          降级说明：{t}
        </div>
      ))}

      <div className="grid grid-cols-1 md:grid-cols-3 gap-x-6 gap-y-3 mt-3">
        <DetailSection title={`产出（${node.files.length}）`}>
          {node.files.length === 0 ? (
            <Empty>本阶段无文件产物</Empty>
          ) : (
            <div className="flex flex-wrap gap-1.5">
              {node.files.map((f) => {
                const a = artifactIndex[f]
                return (
                  <button
                    key={f}
                    onClick={() => onOpenFile(f)}
                    className="text-[10.5px] px-1.5 py-[3px] rounded bg-[#151d2b] hover:bg-[#1e2839] text-[#8b98b4] hover:text-[#c9d5ea] font-mono transition-colors"
                    title={a ? fmtSize(a.size) : f}
                  >
                    {f}
                    {a && <span className="ml-1.5 text-[#5f6d87] not-italic">{fmtSize(a.size)}</span>}
                  </button>
                )
              })}
            </div>
          )}
        </DetailSection>

        <DetailSection title="验收">
          {node.criteriaOk == null ? (
            <Empty>尚无验收结果</Empty>
          ) : node.criteriaOk ? (
            <div className="text-[11.5px] text-emerald-300">全部通过</div>
          ) : (
            <div className="space-y-1">
              <div className="text-[11.5px] text-rose-300">未通过</div>
              {node.failures.map((f, i) => (
                <div key={i} className="text-[10.5px] text-rose-300/80 leading-relaxed">
                  · {f}
                </div>
              ))}
            </div>
          )}
        </DetailSection>

        <DetailSection title={`阶段动作${node.actions.length ? `（${node.actions.length}）` : ''}`}>
          {node.actions.length === 0 ? (
            <Empty>本阶段无工具动作</Empty>
          ) : (
            <div className="space-y-1">
              {node.actions.map((a, i) => (
                <div key={i} className="flex items-start gap-1.5 text-[11px]">
                  <span className={a.ok === false ? 'text-rose-300' : 'text-emerald-300'}>{a.ok === false ? '✕' : '✓'}</span>
                  <span className="text-[#8b98b4]">
                    <span className="text-[#c9d5ea]">{TOOL_LABEL[a.tool] ?? a.tool}</span>
                    {a.summary ? ` · ${a.summary}` : ''}
                    {a.error ? <span className="text-rose-300"> · {a.error}</span> : null}
                  </span>
                </div>
              ))}
            </div>
          )}
        </DetailSection>
      </div>

      {gate && (
        <div className="mt-3.5 rounded-lg border border-amber-500/30 bg-amber-500/[0.07] px-3 py-2.5">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-[11.5px] text-amber-200">
              关卡「{gate.title || node.name}」· {GATE_STATUS[gate.status] ?? gate.status}
            </span>
            {gate.prompt && <span className="text-[10.5px] text-amber-200/70 line-clamp-1">{gate.prompt}</span>}
          </div>
          {gate.status === 'pending' && (
            <div className="flex items-center gap-2 mt-2">
              <input
                value={feedback}
                onChange={(e) => setFeedback(e.target.value)}
                placeholder="给模型的意见（打回 / 返工时用）"
                className="flex-1 text-[11.5px] bg-[#0f1420] border border-[#26324a] rounded-lg px-2.5 py-1.5 text-[#c9d5ea] placeholder:text-[#4c5975] outline-none focus:border-[#4f6bff]/60"
              />
              <button
                disabled={busy}
                onClick={() => onResolveGate(gate.id, 'approved', feedback)}
                className="text-[11.5px] px-3 py-1.5 rounded-lg bg-emerald-500/15 hover:bg-emerald-500/25 text-emerald-300 transition-colors disabled:opacity-50"
              >
                通过
              </button>
              <button
                disabled={busy || !feedback.trim()}
                onClick={() => onResolveGate(gate.id, 'revised', feedback)}
                className="text-[11.5px] px-3 py-1.5 rounded-lg bg-amber-500/15 hover:bg-amber-500/25 text-amber-300 transition-colors disabled:opacity-40"
              >
                要求返工
              </button>
            </div>
          )}
        </div>
      )}

      {/* 画布编辑：改提示词 / 调角色锚点 / 单格重画 / 上传替换 / 阶段级模型 */}
      {editing && (
        <div className="-mx-4 -mb-3.5 mt-3.5">
          <CanvasEditor
            runId={runId}
            stageId={node.stageId}
            stageName={node.name}
            produces={node.files}
            onChanged={() => onRunChanged?.()}
          />
        </div>
      )}

      {/* 本阶段的文件目录（与任务级目录表同一份数据，只筛出这个阶段） */}
      {!editing && node.files.length > 0 && (
        <div className="mt-4">
          <div className="text-[10.5px] text-[#5f6d87] mb-2">本阶段目录 · 每个文件的大小、来源与改动时间</div>
          <DirectoryTable runId={runId} stageFilter={node.stageId} onOpenFile={onOpenFile} compact />
        </div>
      )}
    </div>
  )
}

function DetailSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div>
      <div className="text-[10.5px] text-[#5f6d87] mb-1.5">{title}</div>
      {children}
    </div>
  )
}

function Empty({ children }: { children: ReactNode }) {
  return <div className="text-[10.5px] text-[#4c5975]">{children}</div>
}
