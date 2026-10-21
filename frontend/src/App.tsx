import { useCallback, useEffect, useRef, useState } from 'react'
import { api, openRunStream } from './lib/api'
import type { RunDetail, RunEvent, RunSummary, SkillDetail, SkillSummary } from './lib/types'
import Sidebar from './components/Sidebar'
import CreatePanel from './components/CreatePanel'
import StageTree from './components/StageTree'
import RunCanvas from './components/RunCanvas'
import GateCard from './components/GateCard'
import DraftCard from './components/DraftCard'
import ArtifactPanel from './components/ArtifactPanel'
import DirectoryTable from './components/DirectoryTable'
import DecisionPanel from './components/DecisionPanel'
import EventStream from './components/EventStream'
import FileViewer from './components/FileViewer'
import ModelConfig from './components/ModelConfig'
import { STATUS_TEXT, fmtAgo, statusTone } from './lib/format'

const REFRESH_EVENTS = new Set([
  'stage.started', 'stage.completed', 'stage.revision', 'stage.failed',
  'gate.opened', 'gate.resolved', 'artifact.created',
  'draft.ready', 'run.resumed',
  'run.status', 'run.finished', 'run.error',
])

export default function App() {
  const [meta, setMeta] = useState<{ llm_mode: string; model: string } | null>(null)
  const [skills, setSkills] = useState<SkillSummary[]>([])
  const [activeSkill, setActiveSkill] = useState<SkillDetail | null>(null)
  const [runs, setRuns] = useState<RunSummary[]>([])
  const [currentRunId, setCurrentRunId] = useState<string | null>(null)
  const [run, setRun] = useState<RunDetail | null>(null)
  const [events, setEvents] = useState<RunEvent[]>([])
  const [streamState, setStreamState] = useState<'open' | 'connecting' | 'closed'>('closed')
  const [busy, setBusy] = useState(false)
  const [viewerPath, setViewerPath] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [modelOpen, setModelOpen] = useState(false)

  const refreshTimer = useRef<number | null>(null)

  // ---- 初始加载 ----
  useEffect(() => {
    ;(async () => {
      try {
        const [m, s, r] = await Promise.all([api.meta(), api.skills(), api.runs()])
        setMeta(m)
        setSkills(s.skills)
        setRuns(r.runs)
        if (s.skills.length) await pickSkill(s.skills[0].id)
        if (r.runs.length) setCurrentRunId(r.runs[0].id)
      } catch (e: any) {
        setError(`无法连接后端：${e.message}。请确认后端已在 127.0.0.1:8000 启动。`)
      }
    })()
  }, [])

  const pickSkill = async (id: string) => {
    try {
      setActiveSkill(await api.skill(id))
      setCurrentRunId(null)
    } catch (e: any) {
      setError(e.message)
    }
  }

  // ---- 拉取 Run 详情 ----
  const refreshRun = useCallback(async (id: string) => {
    try {
      setRun(await api.run(id))
    } catch {
      /* 忽略瞬时失败 */
    }
  }, [])

  const scheduleRefresh = useCallback(
    (id: string) => {
      if (refreshTimer.current) window.clearTimeout(refreshTimer.current)
      refreshTimer.current = window.setTimeout(() => refreshRun(id), 180)
    },
    [refreshRun],
  )

  const refreshRuns = useCallback(async () => {
    try {
      setRuns((await api.runs()).runs)
    } catch {
      /* ignore */
    }
  }, [])

  // ---- 订阅事件流（切换 Run 会重建连接）----
  useEffect(() => {
    if (!currentRunId) {
      setEvents([])
      setStreamState('closed')
      return
    }
    setEvents([])
    void refreshRun(currentRunId)

    const close = openRunStream(
      currentRunId,
      (e) => {
        setEvents((prev) => (prev.length > 600 ? [...prev.slice(-500), e] : [...prev, e]))
        if (REFRESH_EVENTS.has(e.type)) {
          scheduleRefresh(currentRunId)
          refreshRuns()
        }
      },
      setStreamState,
    )
    return close
  }, [currentRunId, refreshRun, scheduleRefresh, refreshRuns])

  // ---- 创建任务 ----
  const createRun = async (brief: Record<string, any>, presetId: string | null, channel: 'fast' | 'slow') => {
    if (!activeSkill) return
    setBusy(true)
    setError(null)
    try {
      const created = await api.createRun({
        skill_id: activeSkill.id,
        brief,
        channel,
        preset_id: presetId,
        autostart: true,
      })
      await refreshRuns()
      setActiveSkill(null)
      setCurrentRunId(created.id)
    } catch (e: any) {
      setError(`创建任务失败：${e.message}`)
    } finally {
      setBusy(false)
    }
  }

  const resolveGate = async (gateId: string, status: 'approved' | 'rejected' | 'revised', feedback: string) => {
    if (!currentRunId) return
    setBusy(true)
    try {
      await api.resolveGate(currentRunId, gateId, { status, feedback })
      await refreshRun(currentRunId)
      await refreshRuns()
    } catch (e: any) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const cancelRun = async () => {
    if (!currentRunId) return
    await api.cancelRun(currentRunId).catch(() => {})
    await refreshRun(currentRunId)
    await refreshRuns()
  }

  /** 草稿 → 精修：同一份产物上续跑，不重开任务 */
  const upgradeRun = async () => {
    if (!currentRunId) return
    setBusy(true)
    setError(null)
    try {
      await api.upgradeRun(currentRunId)
      await refreshRun(currentRunId)
      await refreshRuns()
    } catch (e: any) {
      setError(`续跑失败：${e.message}`)
    } finally {
      setBusy(false)
    }
  }

  /** 接着跑：中断/失败后补跑未完成阶段（不换通道、不重做已完成阶段） */
  const resumeRun = async () => {
    if (!currentRunId) return
    setBusy(true)
    setError(null)
    try {
      const r = await api.resumeRun(currentRunId)
      await refreshRun(currentRunId)
      await refreshRuns()
      if (!r.pending_stages.length) setError('没有待补跑的阶段，任务产物已完整。')
    } catch (e: any) {
      setError(`续跑失败：${e.message}`)
    } finally {
      setBusy(false)
    }
  }

  const pendingGate = run?.gates?.find((g) => g.status === 'pending') ?? null
  const tone = run ? statusTone(run.status) : null

  return (
    <div className="h-full flex overflow-hidden">
      <Sidebar
        skills={skills}
        runs={runs}
        currentRunId={currentRunId}
        activeSkillId={activeSkill?.id ?? null}
        meta={meta}
        onSelectRun={(id) => {
          setActiveSkill(null)
          setCurrentRunId(id)
        }}
        onPickSkill={pickSkill}
        onNewTask={() => {
          setCurrentRunId(null)
          if (skills.length) void pickSkill(activeSkill?.id ?? skills[0].id)
        }}
        onOpenModelConfig={() => setModelOpen(true)}
      />

      {/* 中栏 */}
      <main className="flex-1 min-w-0 h-full overflow-y-auto bg-[#070a11]">
        {error && (
          <div className="mx-8 mt-6 rounded-xl border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-[12px] text-rose-200">
            {error}
            <button onClick={() => setError(null)} className="ml-3 underline opacity-70">关闭</button>
          </div>
        )}

        {!currentRunId && activeSkill && (
          <CreatePanel skill={activeSkill} busy={busy} onCreate={createRun} />
        )}

        {!currentRunId && !activeSkill && (
          <div className="h-full flex items-center justify-center text-[13px] text-[#5f6d87]">
            选一个技能开始
          </div>
        )}

        {currentRunId && run && (
          <div className="max-w-[1000px] mx-auto px-8 py-8">
            {/* 头部 */}
            <div className="flex items-start gap-4">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2.5 flex-wrap">
                  <h1 className="text-[19px] font-semibold tracking-tight truncate">{run.title}</h1>
                  <span className={`text-[10.5px] px-2 py-[3px] rounded-full border ${tone?.bg} ${tone?.text} shrink-0`}>
                    {STATUS_TEXT[run.status]}
                  </span>
                  <span className="text-[10.5px] px-2 py-[3px] rounded-full bg-[#1a2233] text-[#7c8aa5] shrink-0">
                    {run.channel === 'fast'
                      ? '3 分钟草稿'
                      : run.origin_channel === 'fast'
                        ? '草稿已升级 · 续跑中'
                        : '精修通道'}
                  </span>
                </div>
                <div className="text-[11.5px] text-[#5f6d87] mt-1.5">
                  {run.skill_name} · {fmtAgo(run.created_at)} · {run.tokens_used} tokens · ${run.cost_usd.toFixed(4)}
                </div>
              </div>

              <div className="flex gap-2 shrink-0">
                <a
                  href={api.downloadUrl(run.id)}
                  className="text-[11.5px] px-3 py-2 rounded-lg bg-[#1a2233] hover:bg-[#24304a] text-[#c9d5ea] transition-colors"
                >
                  下载全部
                </a>
                {['interrupted', 'failed', 'cancelled'].includes(run.status) && (
                  <button
                    disabled={busy}
                    onClick={resumeRun}
                    className="text-[11.5px] px-3 py-2 rounded-lg bg-orange-500/15 hover:bg-orange-500/25 text-orange-200 transition-colors disabled:opacity-40"
                  >
                    续跑
                  </button>
                )}
                {['running', 'awaiting_gate', 'pending'].includes(run.status) && (
                  <button
                    onClick={cancelRun}
                    className="text-[11.5px] px-3 py-2 rounded-lg bg-[#1a2233] hover:bg-[#3a1f28] text-[#c9d5ea] hover:text-rose-300 transition-colors"
                  >
                    停止
                  </button>
                )}
              </div>
            </div>

            {/* 进度条 */}
            <div className="mt-5">
              <div className="flex items-center justify-between text-[11px] text-[#7c8aa5] mb-1.5">
                <span>
                  {run.steps.find((s) => s.status === 'in_progress')?.stage_name ??
                    (() => {
                      const cut = run.steps.find((s) => s.status === 'interrupted')?.stage_name
                      if (cut) return `${cut} · 已中断`
                      return run.status === 'succeeded' ? '全部完成' : '准备中'
                    })()}
                </span>
                <span>{Math.round(run.progress * 100)}%</span>
              </div>
              <div className="h-1.5 rounded-full bg-[#151d2b] overflow-hidden">
                <div
                  className={`h-full rounded-full transition-all duration-500 ${
                    run.status === 'failed'
                      ? 'bg-rose-500'
                      : run.status === 'interrupted'
                        ? 'bg-orange-500'
                        : run.status === 'succeeded'
                          ? 'bg-emerald-500'
                          : 'bg-[#4f6bff]'
                  }`}
                  style={{ width: `${Math.max(run.progress * 100, run.status === 'running' ? 3 : 0)}%` }}
                />
              </div>
            </div>

            {/* 编排画布：一次 Run 的阶段流水线（读 manifest 结构，不硬编码阶段名） */}
            <div className="mt-7">
              <div className="flex items-baseline gap-2.5 mb-3.5">
                <h2 className="text-[13.5px] font-medium">编排画布</h2>
                <span className="text-[10.5px] text-[#5f6d87]">点节点看该阶段的产出、验收与动作</span>
              </div>
              <RunCanvas
                runId={run.id}
                steps={run.steps}
                gates={run.gates}
                events={events}
                artifacts={run.artifacts}
                decisions={run.decisions}
                currentStageId={run.current_stage_id}
                runStatus={run.status}
                busy={busy}
                onResolveGate={(gateId, status, feedback) => resolveGate(gateId, status, feedback)}
                onOpenFile={setViewerPath}
                onRunChanged={() => void refreshRun(run.id)}
              />
            </div>

            {/* 关卡 */}
            {pendingGate && (
              <div className="mt-6">
                <GateCard
                  gate={pendingGate}
                  busy={busy}
                  onResolve={(status, feedback) => resolveGate(pendingGate.id, status, feedback)}
                  onOpenFile={setViewerPath}
                />
              </div>
            )}

            {run.error && run.status !== 'interrupted' && (
              <div className="mt-6 rounded-xl border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-[12px] text-rose-200">
                {run.error}
              </div>
            )}

            {/* 中断横幅：不是失败——产物都在，一步就能续上 */}
            {run.status === 'interrupted' && (
              <div className="mt-6 rounded-xl border border-orange-500/40 bg-orange-500/10 px-4 py-3.5">
                <div className="flex items-start gap-3">
                  <span className="text-[13px] leading-5">⏸</span>
                  <div className="flex-1 min-w-0">
                    <div className="text-[12.5px] text-orange-100 font-medium">任务执行中被中断</div>
                    <div className="text-[11.5px] text-orange-200/85 mt-1 leading-5">
                      {run.error || '后端进程重启，正在执行的阶段被打断。'}
                      <br />
                      已完成 {run.steps.filter((s) => s.status === 'completed').length} 个阶段的产物已保留，
                      点「续跑」会跳过它们、只补跑剩下的阶段，不会从零重来。
                    </div>
                  </div>
                  <button
                    disabled={busy}
                    onClick={resumeRun}
                    className="shrink-0 text-[11.5px] px-3.5 py-2 rounded-lg bg-orange-500/25 hover:bg-orange-500/40 text-orange-50 transition-colors disabled:opacity-40"
                  >
                    {busy ? '启动中…' : '续跑'}
                  </button>
                </div>
              </div>
            )}

            {/* 草稿卡片：交付后（或已进入精修）才出现，不打扰执行中的等待 */}
            {run.draft &&
              (run.status === 'succeeded' || run.resumed_count > 0) && (
                <DraftCard draft={run.draft} busy={busy} onUpgrade={upgradeRun} />
              )}

            {/* 任务目录表：这个任务到底产出了什么、每个文件是谁写的、成片用了哪几个 */}
            <div className="mt-8">
              <div className="flex items-baseline gap-2.5 mb-3.5">
                <h2 className="text-[13.5px] font-medium">任务目录表</h2>
                <span className="text-[10.5px] text-[#5f6d87]">
                  全部产物分层清单，标出阶段与来源，可导出成 INDEX.md
                </span>
              </div>
              <DirectoryTable runId={run.id} onOpenFile={setViewerPath} />
            </div>

            {/* 两列 */}
            <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,0.85fr)_minmax(0,1.15fr)] gap-8 mt-8 pb-16">
              <section>
                <h2 className="text-[13.5px] font-medium mb-3.5">阶段明细</h2>
                <StageTree steps={run.steps} currentStageId={run.current_stage_id} runStatus={run.status} />
              </section>

              <div className="space-y-8">
                <ArtifactPanel runId={run.id} artifacts={run.artifacts} files={run.files} onOpenFile={setViewerPath} />
                <DecisionPanel decisions={run.decisions} />
              </div>
            </div>
          </div>
        )}

        {currentRunId && !run && (
          <div className="h-full flex items-center justify-center text-[13px] text-[#5f6d87]">加载中…</div>
        )}
      </main>

      {/* 右栏 */}
      {currentRunId && <EventStream events={events} streamState={streamState} />}

      {/* 产物预览 */}
      {viewerPath && currentRunId && (
        <FileViewer
          runId={currentRunId}
          relPath={viewerPath}
          onClose={() => setViewerPath(null)}
          onChanged={() => void refreshRun(currentRunId)}
        />
      )}

      {/* 模型配置 */}
      <ModelConfig
        open={modelOpen}
        onClose={() => setModelOpen(false)}
        onSaved={(status) => setMeta({ llm_mode: status.mode, model: status.model })}
      />
    </div>
  )
}
