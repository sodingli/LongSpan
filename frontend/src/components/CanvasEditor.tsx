import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { api } from '../lib/api'
import type { CanvasData, CanvasModels, CanvasPanel, FrameRole } from '../lib/types'
import { fmtSize } from '../lib/format'
import StageFileEditor from './StageFileEditor'

/**
 * 画布编辑器：把"看得见"变成"改得动"。
 *
 * 三件事，对应三种真实诉求：
 *  1. 提示词微调 —— 某一格画得不对，改词重画，不用整片重跑；
 *  2. 人工介入 —— 挑到一张更好的图，直接盖上去（并标注是人工上传，不冒充模型产物）；
 *  3. 阶段级模型 —— 剧本想用强模型、结构化清单想用稳模型，各自指定。
 *
 * 所有改动在服务端留痕，且首次改动前会自动备份模型原件 —— 原件永远查得到。
 */

type Tab = 'panels' | 'characters' | 'files' | 'models'

interface Props {
  runId: string
  stageId: string
  stageName: string
  /** 该阶段已产出的文件（用来决定默认打开哪一页，不靠硬编码阶段名） */
  produces: string[]
  onChanged: () => void
}

const SOURCE_LABEL: Record<string, { text: string; cls: string }> = {
  model: { text: '模型生成', cls: 'bg-emerald-500/12 text-emerald-300' },
  upload: { text: '人工上传', cls: 'bg-violet-500/15 text-violet-300' },
  placeholder: { text: '占位图', cls: 'bg-amber-500/15 text-amber-300' },
}

const ROLE_LABEL: Record<FrameRole, string> = { first: '首帧', mid: '中间帧', last: '尾帧' }

/** 单格修改建议（后端只读，采纳由人决定） */
interface PanelSug {
  index: number
  diagnosis: string
  patch: Record<string, string>
  why: Record<string, string>
  model: string
}
const SUG_FIELD_LABEL: Record<string, string> = {
  prompt: '画面描述',
  scene: '场景 / 景别',
  motion: '镜头运动',
  subtitle: '字幕',
  speaker: '说话人',
}
const ROLE_HINT: Record<FrameRole, string> = {
  first: '这一格从哪开始（也是生成视频的首帧）',
  mid: '动作进行到一半（与首帧同场景）',
  last: '这一格停在哪（有视频时作为尾帧参考）',
}

export default function CanvasEditor({ runId, stageId, stageName, produces, onChanged }: Props) {
  const [data, setData] = useState<CanvasData | null>(null)
  const [models, setModels] = useState<CanvasModels | null>(null)
  const [tab, setTab] = useState<Tab>('models')
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [drafts, setDrafts] = useState<Record<number, Partial<CanvasPanel>>>({})
  const [charsDraft, setCharsDraft] = useState<string | null>(null)
  const [selected, setSelected] = useState<number | null>(null)
  const [videoDur, setVideoDur] = useState(5)
  const [videoMode, setVideoMode] = useState('std')
  const [videoModel, setVideoModel] = useState('') // '' = 全局默认模型
  const [refUrl, setRefUrl] = useState('')
  const [sug, setSug] = useState<PanelSug | null>(null)
  const [sugNote, setSugNote] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [d, m] = await Promise.all([api.canvas(runId), api.canvasModels(runId)])
      setData(d)
      setModels(m)
      setDrafts({})
      setCharsDraft(null)
      setErr(null)
    } catch (e: any) {
      setErr(e.message)
    } finally {
      setLoading(false)
    }
  }, [runId])

  useEffect(() => {
    void load()
  }, [load])

  // 默认页签按"这个阶段产出了什么"决定，而不是按阶段名猜
  useEffect(() => {
    if (produces.includes('panels.json')) setTab('panels')
    else if (produces.includes('characters.md')) setTab('characters')
    else setTab('files')
  }, [stageId, produces.join(',')])

  const panels = data?.panels ?? []
  const dirtyCount = Object.keys(drafts).length

  useEffect(() => {
    if (selected == null && panels.length) setSelected(panels[0].index)
    if (selected != null && !panels.some((p) => p.index === selected)) setSelected(panels[0]?.index ?? null)
  }, [panels, selected])

  // 换格后上一格的建议不再有意义
  useEffect(() => {
    setSug(null)
  }, [selected])

  const current = useMemo(() => panels.find((p) => p.index === selected) ?? null, [panels, selected])
  const merged = useMemo(() => (current ? { ...current, ...(drafts[current.index] ?? {}) } : null), [current, drafts])

  const flash = (t: string) => {
    setMsg(t)
    window.setTimeout(() => setMsg((m) => (m === t ? null : m)), 4000)
  }

  const patchDraft = (index: number, patch: Partial<CanvasPanel>) => {
    setDrafts((d) => ({ ...d, [index]: { ...(d[index] ?? {}), ...patch } }))
  }

  // ---------------- 大模型修改建议（只读 → 人采纳）----------------

  const askSuggestion = async (index: number, note = '') => {
    setBusy(`sug-${index}`)
    setErr(null)
    try {
      const res = await api.suggestPanel(runId, index, { note })
      setSug({ index: res.index, diagnosis: res.diagnosis, patch: res.patch, why: res.why, model: res.model })
    } catch (e: any) {
      setErr(e.message)
    } finally {
      setBusy(null)
    }
  }

  const applySuggestion = () => {
    if (!sug) return
    patchDraft(sug.index, sug.patch as Partial<CanvasPanel>)
    flash(`已把第 ${sug.index} 格的建议填进编辑框 —— 确认后点「保存本格」生效`)
    setSug(null)
    setSugNote('')
  }

  // ---------------- 操作 ----------------

  const saveDrafts = async (only?: number) => {
    const payload = (only != null ? (drafts[only] ? [only] : []) : Object.keys(drafts).map(Number)).map((i) => ({
      index: i,
      ...(drafts[i] ?? {}),
    }))
    if (!payload.length) {
      flash('没有需要保存的改动')
      return
    }
    setBusy('save')
    try {
      const res = await api.savePanels(runId, payload)
      flash(res.note ? res.note : `已保存 ${res.edited.length} 格改动（模型原件已备份）`)
      await load()
      onChanged()
    } catch (e: any) {
      setErr(e.message)
    } finally {
      setBusy(null)
    }
  }

  const regenerate = async (index: number, useDraft = true) => {
    const d = useDraft ? drafts[index] : undefined
    setBusy(`regen-${index}`)
    setErr(null)
    try {
      const res = await api.regeneratePanel(runId, index, {
        stage_id: stageId,
        prompt: d?.prompt,
        ratio: d?.ratio,
      })
      flash(
        res.ok
          ? `第 ${index} 格已重画（${res.model || '默认模型'}）`
          : `第 ${index} 格降级为占位图：生图服务未生效`,
      )
      await load()
      onChanged()
    } catch (e: any) {
      setErr(e.message)
    } finally {
      setBusy(null)
    }
  }

  const uploadFor = (index: number, file: File) => {
    const reader = new FileReader()
    reader.onload = async () => {
      setBusy(`upload-${index}`)
      setErr(null)
      try {
        const res = await api.uploadPanelImage(runId, index, {
          data_url: String(reader.result),
          note: `画布人工替换第 ${index} 格`,
        })
        flash(`第 ${index} 格已替换为上传图片（${fmtSize(res.size)}）`)
        await load()
        onChanged()
      } catch (e: any) {
        setErr(e.message)
      } finally {
        setBusy(null)
      }
    }
    reader.readAsDataURL(file)
  }

  const saveChars = async () => {
    if (charsDraft == null) return
    setBusy('chars')
    try {
      await api.saveCharacters(runId, charsDraft)
      flash('角色卡与一致性锚点已保存 —— 下一轮生图会用新锚点')
      await load()
      onChanged()
    } catch (e: any) {
      setErr(e.message)
    } finally {
      setBusy(null)
    }
  }

  const restoreChars = async () => {
    setBusy('chars-restore')
    try {
      const res = await api.charactersOrigin(runId)
      setCharsDraft(res.text)
      flash('已载入模型原版，确认后点保存生效')
    } catch (e: any) {
      setErr(e.message)
    } finally {
      setBusy(null)
    }
  }

  const saveOverride = async (patch: Record<string, string>) => {
    setBusy('ov')
    try {
      const res = await api.setStageOverride(runId, stageId, patch)
      flash(res.changed.length ? `本阶段模型已指定：${res.changed.join('、')}` : '本阶段恢复为全局配置')
      await load()
      onChanged()
    } catch (e: any) {
      setErr(e.message)
    } finally {
      setBusy(null)
    }
  }

  // ---------------- 多帧 / 视频 ----------------

  const uploadFrameFor = (index: number, role: FrameRole, file: File) => {
    const reader = new FileReader()
    reader.onload = async () => {
      setBusy(`frame-${index}-${role}`)
      setErr(null)
      try {
        await api.uploadFrame(runId, index, role, {
          data_url: String(reader.result),
          note: `画布人工上传第 ${index} 格${role}`,
        })
        flash(`第 ${index} 格${ROLE_LABEL[role]}已上传`)
        await load()
        onChanged()
      } catch (e: any) {
        setErr(e.message)
      } finally {
        setBusy(null)
      }
    }
    reader.readAsDataURL(file)
  }

  const regenFrame = async (index: number, role: FrameRole) => {
    setBusy(`frame-${index}-${role}`)
    setErr(null)
    try {
      const res = await api.regenerateFrame(runId, index, role, {
        stage_id: stageId,
        prompt: drafts[index]?.prompt,
      })
      flash(res.ok ? `第 ${index} 格${ROLE_LABEL[role]}已生成` : `第 ${index} 格${ROLE_LABEL[role]}降级为占位图`)
      await load()
      onChanged()
    } catch (e: any) {
      setErr(e.message)
    } finally {
      setBusy(null)
    }
  }

  const removeFrame = async (index: number, role: FrameRole) => {
    setBusy(`frame-${index}-${role}`)
    try {
      await api.deleteFrame(runId, index, role)
      flash(`已删除第 ${index} 格${ROLE_LABEL[role]}`)
      await load()
      onChanged()
    } catch (e: any) {
      setErr(e.message)
    } finally {
      setBusy(null)
    }
  }

  const uploadReference = (index: number, file: File) => {
    const reader = new FileReader()
    reader.onload = async () => {
      setBusy(`ref-${index}`)
      setErr(null)
      try {
        await api.uploadReferenceVideo(runId, index, { data_url: String(reader.result) })
        flash(`第 ${index} 格参考视频已上传`)
        await load()
        onChanged()
      } catch (e: any) {
        setErr(e.message)
      } finally {
        setBusy(null)
      }
    }
    reader.readAsDataURL(file)
  }

  /** 参考视频也可以直接给公网 URL —— 大文件（>6MB）内联会被拒，这条路是必须的 */
  const setReferenceUrl = async (index: number, url: string) => {
    const v = url.trim()
    if (!v) return
    setBusy(`ref-${index}`)
    setErr(null)
    try {
      await api.uploadReferenceVideo(runId, index, { url: v })
      flash(`第 ${index} 格参考视频已设为公网地址`)
      setRefUrl('')
      await load()
      onChanged()
    } catch (e: any) {
      setErr(e.message)
    } finally {
      setBusy(null)
    }
  }

  const generateVideo = async (index: number) => {
    setBusy(`video-${index}`)
    setErr(null)
    try {
      const r = await api.generatePanelVideo(runId, index, { duration: videoDur, mode: videoMode, model: videoModel || undefined })
      flash(r.message)
      await load()
      onChanged()
    } catch (e: any) {
      setErr(e.message)
    } finally {
      setBusy(null)
    }
  }

  /** 一键为所有还没有视频片段的镜头提交生成。逐格调用既有接口，
   *  每格都会走同样的预检（首帧存在、未在生成中）；费用必须先确认 ——
   *  视频按次计费，自动跑 6 格 = 6 次真实支出，不能悄悄花钱。 */
  const generateAllVideos = async () => {
    if (!data?.video.ready) return
    const targets = panels.filter(
      (p) => (p.frames?.first || p.image) && p.video_status !== 'ready' && p.video_status !== 'running',
    )
    if (!targets.length) {
      flash('所有镜头都已有视频片段（或没有首帧），没有需要生成的')
      return
    }
    const ok = window.confirm(
      `将为 ${targets.length} 个镜头各提交一次视频生成（模型：${videoModel || data.video.model} · ${videoDur}s）。` +
        `\n视频按次计费：本次预计产生 ${targets.length} 次调用，几分钟内陆续完成。确定继续？`,
    )
    if (!ok) return
    setBusy('video-all')
    setErr(null)
    let okN = 0
    const fails: string[] = []
    for (const p of targets) {
      try {
        await api.generatePanelVideo(runId, p.index, {
          duration: videoDur,
          mode: videoMode,
          model: videoModel || undefined,
        })
        okN++
      } catch (e: any) {
        fails.push(`格${p.index}: ${e.message}`)
      }
    }
    setBusy(null)
    flash(
      `已提交 ${okN}/${targets.length} 个镜头的视频生成` +
        (fails.length ? `；失败：${fails.join('；')}` : '。全部完成后重新渲染成片即可用上'),
    )
    await load()
    onChanged()
  }

  const removeVideo = async (index: number) => {    setBusy(`video-${index}`)
    try {
      await api.deletePanelVideo(runId, index)
      flash(`已移除第 ${index} 格的视频片段`)
      await load()
      onChanged()
    } catch (e: any) {
      setErr(e.message)
    } finally {
      setBusy(null)
    }
  }

  // 有视频任务在跑时自动轮询 —— 视频是分钟级的，用户不该手动刷新页面
  const anyRunning = panels.some((p) => p.video_status === 'running')
  useEffect(() => {
    if (!anyRunning) return
    const t = window.setInterval(() => void load(), 6000)
    return () => window.clearInterval(t)
  }, [anyRunning, load])

  // 视频配置默认值跟随服务端（5s / std），用户改过就不再覆盖
  useEffect(() => {
    if (!data) return
    setVideoDur((d) => d || data.video.default_duration || 5)
    setVideoMode((m) => m || data.video.mode || 'std')
  }, [data?.video.default_duration, data?.video.mode])

  const ov = data?.overrides?.[stageId] ?? {}

  const TABS: { id: Tab; label: string; hint?: string }[] = [
    { id: 'panels', label: `分镜提示词${panels.length ? ` · ${panels.length}` : ''}` },
    { id: 'characters', label: '角色与锚点' },
    { id: 'files', label: '步骤产物' },
    { id: 'models', label: '本阶段模型' },
  ]

  return (
    <div className="border-t border-[#1e2839] bg-[#080b12]">
      {/* 工具条 */}
      <div className="flex items-center gap-1.5 px-4 pt-3 flex-wrap">
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`text-[11.5px] px-2.5 py-1.5 rounded-lg transition-colors ${
              tab === t.id ? 'bg-[#1a2233] text-[#dbe4f5]' : 'text-[#6f7d99] hover:text-[#a8b6d1]'
            }`}
          >
            {t.label}
          </button>
        ))}
        <span className="ml-auto text-[10.5px] text-[#4c5975]">
          {loading ? '加载中…' : data ? `已留痕：改词 ${data.edits.panels ?? 0} · 重画 ${data.edits.regenerate ?? 0} · 上传 ${data.edits.upload ?? 0}` : ''}
        </span>
      </div>

      {(err || msg) && (
        <div className="px-4 pt-2.5">
          {err && (
            <div className="rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-1.5 text-[11px] text-rose-200">
              {err}
            </div>
          )}
          {msg && (
            <div className="rounded-lg border border-emerald-500/35 bg-emerald-500/10 px-3 py-1.5 text-[11px] text-emerald-200">
              {msg}
            </div>
          )}
        </div>
      )}

      <div className="px-4 py-3">
        {/* ---------------- 分镜提示词 ---------------- */}
        {tab === 'panels' &&
          (panels.length === 0 ? (
            <div className="text-[11.5px] text-[#5f6d87] py-3">
              本任务还没有分镜清单（panels.json）。跑完「分镜绘制」阶段后即可在这里逐格微调。
            </div>
          ) : (
            <>
              <div className="flex items-center gap-2 mb-2.5 flex-wrap">
                <span className="text-[11px] text-[#6f7d99]">
                  点缩略图选中某格 → 改画面描述 → 只重画这一格
                </span>
                {dirtyCount > 0 && (
                  <span className="text-[10.5px] px-1.5 py-[2px] rounded bg-orange-500/15 text-orange-300">
                    {dirtyCount} 格待保存
                  </span>
                )}
                {data?.video.ready && panels.length > 0 && (
                  <button
                    disabled={busy != null}
                    onClick={() => void generateAllVideos()}
                    title="为还没有视频片段的镜头批量提交视频生成（按次计费，会先确认）"
                    className="ml-auto text-[11.5px] px-3 py-1.5 rounded-lg bg-emerald-500/15 hover:bg-emerald-500/25 text-emerald-200 border border-emerald-500/30 transition-colors disabled:opacity-40"
                  >
                    {busy === 'video-all' ? '提交中…' : '全部镜头生成视频'}
                  </button>
                )}
                <button
                  disabled={busy != null || dirtyCount === 0}
                  onClick={() => saveDrafts()}
                  className={`${data?.video.ready && panels.length > 0 ? '' : 'ml-auto'} text-[11.5px] px-3 py-1.5 rounded-lg bg-[#1a2233] hover:bg-[#24304a] text-[#c9d5ea] transition-colors disabled:opacity-40`}
                >
                  保存全部改动
                </button>
              </div>

              <div className="grid grid-cols-2 sm:grid-cols-3 xl:grid-cols-4 gap-2.5">
                {panels.map((p) => {
                  const draft = drafts[p.index]
                  const isDirty = !!draft
                  const src = SOURCE_LABEL[p.image_source || 'model'] ?? SOURCE_LABEL.model
                  return (
                    <button
                      key={p.index}
                      onClick={() => setSelected(p.index)}
                      className={`text-left rounded-lg overflow-hidden border transition-all ${
                        selected === p.index ? 'border-[#4f6bff] ring-1 ring-[#4f6bff]/50' : 'border-[#1e2839] hover:border-[#3a4a6b]'
                      }`}
                    >
                      <div className="relative bg-[#0f1420] aspect-[9/16] overflow-hidden">
                        {p.image ? (
                          <img src={api.fileUrl(runId, p.image)} alt={`第 ${p.index} 格`} className="w-full h-full object-cover" />
                        ) : (
                          <div className="w-full h-full flex items-center justify-center text-[10.5px] text-[#4c5975]">
                            未出图
                          </div>
                        )}
                        <span className="absolute top-1.5 left-1.5 text-[9.5px] px-1.5 py-[2px] rounded bg-black/65 text-[#dbe4f5]">
                          格 {p.index}
                        </span>
                        {isDirty && (
                          <span className="absolute top-1.5 right-1.5 text-[9.5px] px-1.5 py-[2px] rounded bg-orange-500/85 text-white">
                            已改
                          </span>
                        )}
                      </div>
                      <div className="px-2 py-1.5">
                        <div className="flex items-center gap-1 flex-wrap">
                          <span className={`text-[9.5px] px-1.5 py-[1px] rounded ${src.cls}`}>{src.text}</span>
                          {p.frame_count > 1 && (
                            <span className="text-[9px] px-1 py-[1px] rounded bg-sky-500/15 text-sky-300">
                              {p.frame_count} 帧
                            </span>
                          )}
                          {p.video_status === 'ready' && (
                            <span className="text-[9px] px-1 py-[1px] rounded bg-emerald-500/20 text-emerald-300">视频</span>
                          )}
                          {p.video_status === 'running' && (
                            <span className="text-[9px] px-1 py-[1px] rounded bg-[#4f6bff]/25 text-[#a9baff]">生成中</span>
                          )}
                          {p.video_status === 'failed' && (
                            <span className="text-[9px] px-1 py-[1px] rounded bg-rose-500/20 text-rose-300">视频失败</span>
                          )}
                        </div>
                        <div className="text-[10px] text-[#6f7d99] mt-1 line-clamp-1">{p.scene || `格 ${p.index}`}</div>
                      </div>
                    </button>
                  )
                })}
              </div>

              {/* 单格编辑区 */}
              {merged && (
                <div className="mt-3 rounded-lg border border-[#1e2839] bg-[#0b0f17] p-3">
                  <div className="flex items-center gap-2 mb-2 flex-wrap">
                    <span className="text-[12px] font-medium text-[#dbe4f5]">第 {merged.index} 格</span>
                    {merged.speaker && <span className="text-[10.5px] text-[#8b98b4]">{merged.speaker}</span>}
                    <span className="text-[10.5px] text-[#5f6d87]">
                      {merged.image_provider} {merged.image_bytes ? `· ${fmtSize(merged.image_bytes)}` : ''}
                    </span>
                  </div>

                  <label className="block text-[10.5px] text-[#5f6d87] mb-1">画面描述（提示词）</label>
                  <textarea
                    value={merged.prompt ?? ''}
                    onChange={(e) => patchDraft(merged.index, { prompt: e.target.value })}
                    rows={5}
                    className="w-full text-[11.5px] leading-relaxed bg-[#0f1420] border border-[#26324a] rounded-lg px-2.5 py-2 text-[#dbe4f5] outline-none focus:border-[#4f6bff]/60 resize-y"
                  />

                  <div className="grid grid-cols-2 lg:grid-cols-4 gap-2 mt-2">
                    <Field label="场景 / 景别">
                      <input
                        value={merged.scene ?? ''}
                        onChange={(e) => patchDraft(merged.index, { scene: e.target.value })}
                        className={INPUT}
                      />
                    </Field>
                    <Field label="镜头运动">
                      <input
                        value={merged.motion ?? ''}
                        onChange={(e) => patchDraft(merged.index, { motion: e.target.value })}
                        className={INPUT}
                      />
                    </Field>
                    <Field label="字幕">
                      <input
                        value={merged.subtitle ?? ''}
                        onChange={(e) => patchDraft(merged.index, { subtitle: e.target.value })}
                        className={INPUT}
                      />
                    </Field>
                    <Field label="说话人">
                      <input
                        value={merged.speaker ?? ''}
                        onChange={(e) => patchDraft(merged.index, { speaker: e.target.value })}
                        className={INPUT}
                      />
                    </Field>
                  </div>

                  {/* ---------------- 多帧：首 / 中 / 尾 ---------------- */}
                  <div className="mt-3 rounded-lg border border-[#1e2839] bg-[#0a0e16] p-2.5">
                    <div className="flex items-center gap-2 mb-2 flex-wrap">
                      <span className="text-[11px] text-[#8b98b4]">多帧画面</span>
                      <span className="text-[10px] text-[#4c5975]">
                        挂多张时成片按「首→中→尾」交叉过渡；只挂首帧就是单图 + 运镜
                      </span>
                    </div>
                    <div className="grid grid-cols-3 gap-2">
                      {(data?.frame_roles ?? (['first', 'mid', 'last'] as FrameRole[]).map((r) => ({ id: r, label: ROLE_LABEL[r] }))).map(
                        (fr) => {
                          const role = fr.id as FrameRole
                          const f = merged.frames?.[role] ?? null
                          const key = `frame-${merged!.index}-${role}`
                          return (
                            <div key={role} className="rounded-lg border border-[#1e2839] bg-[#0f1420] overflow-hidden">
                              <div className="relative aspect-[9/16] bg-[#080b12]">
                                {f ? (
                                  <img
                                    src={api.fileUrl(runId, f.path)}
                                    alt={fr.label}
                                    className="w-full h-full object-cover"
                                  />
                                ) : (
                                  <div className="w-full h-full flex items-center justify-center text-[10px] text-[#3f4a60]">
                                    未设置
                                  </div>
                                )}
                                <span className="absolute top-1 left-1 text-[9px] px-1 py-[1px] rounded bg-black/70 text-[#c9d5ea]">
                                  {fr.label}
                                </span>
                                {f?.source === 'upload' && (
                                  <span className="absolute bottom-1 left-1 text-[8.5px] px-1 py-[1px] rounded bg-violet-500/80 text-white">
                                    人工
                                  </span>
                                )}
                              </div>
                              <div className="px-1.5 py-1.5">
                                <div className="flex flex-wrap gap-1">
                                  <button
                                    disabled={busy != null}
                                    onClick={() => regenFrame(merged!.index, role)}
                                    className="text-[9.5px] px-1.5 py-[3px] rounded bg-[#4f6bff]/20 hover:bg-[#4f6bff]/30 text-[#a9baff] disabled:opacity-40"
                                  >
                                    {busy === key ? '…' : '生成'}
                                  </button>
                                  <label className="text-[9.5px] px-1.5 py-[3px] rounded bg-violet-500/15 hover:bg-violet-500/25 text-violet-300 cursor-pointer">
                                    上传
                                    <input
                                      type="file"
                                      accept="image/png,image/jpeg,image/webp"
                                      className="hidden"
                                      onChange={(e) => {
                                        const file = e.target.files?.[0]
                                        if (file) uploadFrameFor(merged!.index, role, file)
                                        e.target.value = ''
                                      }}
                                    />
                                  </label>
                                  {f && (
                                    <button
                                      disabled={busy != null}
                                      onClick={() => removeFrame(merged!.index, role)}
                                      className="text-[9.5px] px-1.5 py-[3px] rounded text-[#6f7d99] hover:text-rose-300 disabled:opacity-40"
                                    >
                                      清除
                                    </button>
                                  )}
                                </div>
                                <div className="text-[8.5px] text-[#3f4a60] mt-1 leading-tight">{ROLE_HINT[role]}</div>
                              </div>
                            </div>
                          )
                        },
                      )}
                    </div>
                  </div>

                  {/* ---------------- 视频片段 ---------------- */}
                  <div className="mt-2.5 rounded-lg border border-[#1e2839] bg-[#0a0e16] p-2.5">
                    <div className="flex items-center gap-2 mb-2 flex-wrap">
                      <span className="text-[11px] text-[#8b98b4]">视频片段</span>
                      <span className="text-[10px] text-[#4c5975]">
                        用首帧（+尾帧/参考视频）生成一段会动的镜头；生成后成片优先用它
                      </span>
                      {!data?.video.ready && (
                        <span className="text-[9.5px] px-1.5 py-[1px] rounded bg-amber-500/15 text-amber-300">
                          未启用
                        </span>
                      )}
                    </div>

                    {merged.video?.status === 'running' && (
                      <div className="rounded-lg border border-[#4f6bff]/40 bg-[#4f6bff]/10 px-2.5 py-1.5 text-[10.5px] text-[#a9baff] mb-2">
                        生成中…（{merged.video.duration ?? videoDur}s / {merged.video.mode ?? videoMode}）
                        几分钟后自动刷新，关掉页面也不丢
                      </div>
                    )}
                    {merged.video?.status === 'failed' && (
                      <div className="rounded-lg border border-rose-500/40 bg-rose-500/10 px-2.5 py-1.5 text-[10.5px] text-rose-200 mb-2 break-words">
                        生成失败：{merged.video.error?.slice(0, 240)}
                        <button
                          onClick={() => generateVideo(merged!.index)}
                          className="ml-2 underline text-rose-100 hover:text-white"
                        >
                          重试
                        </button>
                      </div>
                    )}
                    {merged.video?.status === 'ready' && merged.video.path && (
                      <div className="mb-2">
                        <video
                          src={api.fileUrl(runId, merged.video.path)}
                          controls
                          className="w-full max-h-[260px] rounded-lg border border-[#1e2839] bg-black"
                        />
                        <div className="text-[9.5px] text-[#4c5975] mt-1 break-all">
                          {merged.video.path} · {merged.video.duration}s · {merged.video.mode} ·{' '}
                          {merged.video.reference ? '含参考视频' : merged.video.frames?.length && merged.video.frames.length > 1 ? `多帧 ${merged.video.frames.length}` : '首帧'}
                        </div>
                        {merged.video.note && (
                          <div className="text-[9.5px] text-amber-300/80 mt-1 break-words">
                            {merged.video.note}
                          </div>
                        )}
                      </div>
                    )}

                    <div className="flex items-center gap-1.5 flex-wrap">
                      <label className="text-[10px] text-[#5f6d87]">模型</label>
                      <select
                        value={videoModel}
                        onChange={(e) => setVideoModel(e.target.value)}
                        className="text-[10.5px] bg-[#0f1420] border border-[#26324a] rounded px-1.5 py-1 text-[#dbe4f5] outline-none max-w-[170px]"
                      >
                        <option value="">默认（{data?.video.model || '未配置'}）</option>
                        {(data?.video.models ?? [])
                          .filter((m) => m !== data?.video.model)
                          .map((m) => (
                            <option key={m} value={m}>
                              {m}
                            </option>
                          ))}
                      </select>
                      <label className="text-[10px] text-[#5f6d87]">时长</label>
                      <select
                        value={videoDur}
                        onChange={(e) => setVideoDur(Number(e.target.value))}
                        className="text-[10.5px] bg-[#0f1420] border border-[#26324a] rounded px-1.5 py-1 text-[#dbe4f5] outline-none"
                      >
                        {(data?.video.durations ?? [5, 10]).map((d) => (
                          <option key={d} value={d}>
                            {d}s
                          </option>
                        ))}
                      </select>
                      <label className="text-[10px] text-[#5f6d87]">模式</label>
                      <select
                        value={videoMode}
                        onChange={(e) => setVideoMode(e.target.value)}
                        className="text-[10.5px] bg-[#0f1420] border border-[#26324a] rounded px-1.5 py-1 text-[#dbe4f5] outline-none"
                      >
                        <option value="std">标准</option>
                        <option value="pro">专家</option>
                      </select>
                      <button
                        disabled={busy != null || !data?.video.ready || merged.video?.status === 'running' || !merged.frames?.first}
                        onClick={() => generateVideo(merged!.index)}
                        title={
                          !data?.video.ready
                            ? data?.video.reason || '视频生成未启用'
                            : !merged.frames?.first
                              ? '先生成或上传首帧'
                              : '按次计费，需要等几分钟'
                        }
                        className="text-[10.5px] px-2.5 py-1 rounded bg-emerald-500/15 hover:bg-emerald-500/25 text-emerald-300 disabled:opacity-40"
                      >
                        {busy === `video-${merged.index}` ? '提交中…' : merged.video?.status === 'ready' ? '重新生成视频' : '生成视频'}
                      </button>
                      <label className="text-[10.5px] px-2.5 py-1 rounded bg-violet-500/15 hover:bg-violet-500/25 text-violet-300 cursor-pointer">
                        {merged.reference_video ? '换参考视频' : '上传参考视频'}
                        <input
                          type="file"
                          accept="video/mp4,video/quicktime,video/webm"
                          className="hidden"
                          onChange={(e) => {
                            const file = e.target.files?.[0]
                            if (file) uploadReference(merged!.index, file)
                            e.target.value = ''
                          }}
                        />
                      </label>
                      <input
                        value={refUrl}
                        onChange={(e) => setRefUrl(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') setReferenceUrl(merged!.index, refUrl)
                        }}
                        placeholder="或粘贴公网视频 URL"
                        title="参考视频超过 6MB 时无法内联，请用公网地址"
                        className="text-[10.5px] w-[150px] bg-[#0f1420] border border-[#26324a] rounded px-1.5 py-1 text-[#dbe4f5] outline-none placeholder:text-[#44506b]"
                      />
                      {refUrl.trim() && (
                        <button
                          onClick={() => setReferenceUrl(merged!.index, refUrl)}
                          disabled={busy != null}
                          className="text-[10.5px] px-2 py-1 rounded bg-violet-500/20 hover:bg-violet-500/30 text-violet-200 disabled:opacity-40"
                        >
                          用此 URL
                        </button>
                      )}
                      {merged.video?.status === 'ready' && (
                        <button
                          onClick={() => removeVideo(merged!.index)}
                          disabled={busy != null}
                          className="text-[10.5px] px-2 py-1 rounded text-[#6f7d99] hover:text-rose-300 disabled:opacity-40"
                        >
                          移除片段
                        </button>
                      )}
                    </div>
                    {merged.reference_video && (
                      <div className="text-[9.5px] text-[#4c5975] mt-1 break-all">
                        参考视频：{merged.reference_video}
                        <button
                          onClick={async () => {
                            await api.deleteReferenceVideo(runId, merged!.index)
                            await load()
                          }}
                          className="ml-1.5 text-[#6f7d99] hover:text-rose-300"
                        >
                          移除
                        </button>
                      </div>
                    )}
                    {!data?.video.ready && data?.video.reason && (
                      <div className="text-[9.5px] text-[#4c5975] mt-1">{data.video.reason} —— 到「模型配置 → 视频」里启用</div>
                    )}
                  </div>

                  <div className="flex items-center gap-2 mt-2.5 flex-wrap">
                    <button
                      disabled={busy != null}
                      onClick={() => saveDrafts(merged.index)}
                      className="text-[11.5px] px-3 py-1.5 rounded-lg bg-[#1a2233] hover:bg-[#24304a] text-[#c9d5ea] transition-colors disabled:opacity-40"
                    >
                      保存本格
                    </button>
                    <button
                      disabled={busy != null}
                      onClick={() => regenerate(merged.index)}
                      className="text-[11.5px] px-3 py-1.5 rounded-lg bg-[#4f6bff]/20 hover:bg-[#4f6bff]/30 text-[#a9baff] transition-colors disabled:opacity-40"
                    >
                      {busy === `regen-${merged.index}` ? '重画中…' : '只重画这一格'}
                    </button>
                    <button
                      disabled={busy != null}
                      onClick={() => askSuggestion(merged.index, sugNote)}
                      className="text-[11.5px] px-3 py-1.5 rounded-lg bg-teal-500/15 hover:bg-teal-500/25 text-teal-300 transition-colors disabled:opacity-40"
                    >
                      {busy === `sug-${merged.index}` ? '思考中…' : 'AI 建议'}
                    </button>
                    <label className="text-[11.5px] px-3 py-1.5 rounded-lg bg-violet-500/15 hover:bg-violet-500/25 text-violet-300 cursor-pointer transition-colors">
                      {busy === `upload-${merged.index}` ? '上传中…' : '上传图片替换'}
                      <input
                        type="file"
                        accept="image/png,image/jpeg,image/webp"
                        className="hidden"
                        onChange={(e) => {
                          const f = e.target.files?.[0]
                          if (f) uploadFor(merged.index, f)
                          e.target.value = ''
                        }}
                      />
                    </label>
                    {ov.image_model && (
                      <span className="text-[10.5px] text-[#5f6d87]">
                        本阶段生图模型：<span className="font-mono text-[#8b98b4]">{ov.image_model}</span>
                      </span>
                    )}
                  </div>

                  {/* ---------------- AI 修改建议（只读 → 采纳）---------------- */}
                  {sug && sug.index === merged.index && (
                    <div className="mt-2.5 rounded-lg border border-teal-500/25 bg-teal-500/[0.04] p-3">
                      <div className="flex items-center gap-2 mb-1.5 flex-wrap">
                        <span className="text-[11px] font-medium text-teal-300">AI 诊断</span>
                        <span className="text-[9.5px] text-[#5f6d87]">{sug.model}</span>
                      </div>
                      <p className="text-[11px] leading-relaxed text-[#c9d5ea]">{sug.diagnosis}</p>
                      <div className="mt-2 space-y-1.5">
                        {Object.entries(sug.patch).map(([k, v]) => (
                          <div key={k} className="rounded-md bg-[#0f1420] border border-[#1e2839] px-2.5 py-1.5">
                            <div className="text-[10px] text-teal-300/80 mb-0.5">
                              {SUG_FIELD_LABEL[k] ?? k}
                              {sug.why[k] && <span className="text-[#5f6d87]"> · {sug.why[k]}</span>}
                            </div>
                            <div className="text-[10.5px] leading-relaxed text-[#9fb0cc] line-clamp-4 break-all">{v}</div>
                          </div>
                        ))}
                      </div>
                      <div className="flex items-center gap-2 mt-2 flex-wrap">
                        <button
                          disabled={busy != null}
                          onClick={applySuggestion}
                          className="text-[11px] px-2.5 py-1 rounded-lg bg-teal-500/20 hover:bg-teal-500/30 text-teal-300 disabled:opacity-40"
                        >
                          采纳（填进编辑框，不直接保存）
                        </button>
                        <input
                          value={sugNote}
                          onChange={(e) => setSugNote(e.target.value)}
                          placeholder="告诉它你的要求，例如：表情更惊恐一点"
                          className="flex-1 min-w-[180px] text-[10.5px] bg-[#0f1420] border border-[#26324a] rounded-lg px-2 py-1 text-[#dbe4f5] outline-none focus:border-teal-500/50"
                        />
                        <button
                          disabled={busy != null}
                          onClick={() => askSuggestion(merged.index, sugNote)}
                          className="text-[11px] px-2.5 py-1 rounded-lg bg-[#1a2233] hover:bg-[#24304a] text-[#c9d5ea] disabled:opacity-40"
                        >
                          {busy === `sug-${merged.index}` ? '…' : '按此要求重新建议'}
                        </button>
                        <button
                          onClick={() => setSug(null)}
                          className="text-[11px] px-2 py-1 rounded-lg text-[#6f7d99] hover:text-rose-300"
                        >
                          忽略
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              )}
            </>
          ))}

        {/* ---------------- 角色与锚点 ---------------- */}
        {tab === 'characters' &&
          (data && !data.characters.text ? (
            <div className="text-[11.5px] text-[#5f6d87] py-3">
              本任务还没有角色卡（characters.md）。跑完「角色设计」阶段后即可在这里调锚点。
            </div>
          ) : (
            <div>
              <div className="flex items-center gap-2 mb-2 flex-wrap">
                <span className="text-[11px] text-[#6f7d99]">
                  锚点是冻结字符串，会被原文粘进每一格生图描述 —— 改它等于改角色的长相
                </span>
                {data?.characters.roles.map((r) => (
                  <span key={r} className="text-[10.5px] px-1.5 py-[2px] rounded bg-[#151d2b] text-[#8b98b4]">
                    {r}
                  </span>
                ))}
              </div>
              <textarea
                value={charsDraft ?? data?.characters.text ?? ''}
                onChange={(e) => setCharsDraft(e.target.value)}
                rows={14}
                spellCheck={false}
                className="w-full text-[11.5px] leading-relaxed bg-[#0f1420] border border-[#26324a] rounded-lg px-3 py-2.5 text-[#dbe4f5] outline-none focus:border-[#4f6bff]/60 resize-y"
                style={{ fontFamily: 'var(--font-mono, ui-monospace, monospace)' }}
              />
              <div className="flex items-center gap-2 mt-2.5 flex-wrap">
                <button
                  disabled={busy != null || charsDraft == null}
                  onClick={saveChars}
                  className="text-[11.5px] px-3 py-1.5 rounded-lg bg-[#4f6bff]/20 hover:bg-[#4f6bff]/30 text-[#a9baff] transition-colors disabled:opacity-40"
                >
                  保存角色卡
                </button>
                <button
                  disabled={busy != null || !data?.characters.has_origin}
                  onClick={restoreChars}
                  className="text-[11.5px] px-3 py-1.5 rounded-lg bg-[#1a2233] hover:bg-[#24304a] text-[#c9d5ea] transition-colors disabled:opacity-40"
                  title={data?.characters.has_origin ? '把模型原始版本载入编辑器' : '还没被改过，没有备份原件'}
                >
                  载入模型原版
                </button>
                {charsDraft != null && (
                  <button
                    onClick={() => setCharsDraft(null)}
                    className="text-[11.5px] px-3 py-1.5 rounded-lg text-[#6f7d99] hover:text-[#a8b6d1]"
                  >
                    放弃改动
                  </button>
                )}
                {data?.characters.has_origin && (
                  <span className="text-[10.5px] text-[#5f6d87]">模型原件已备份，随时可对照</span>
                )}
              </div>
            </div>
          ))}

        {/* ---------------- 步骤产物（任何阶段都能改） ---------------- */}
        {tab === 'files' && <StageFileEditor runId={runId} stageId={stageId} onChanged={onChanged} />}

        {/* ---------------- 本阶段模型 ---------------- */}
        {tab === 'models' && (
          <div>
            <p className="text-[11px] text-[#6f7d99] mb-3 leading-relaxed">
              给「{stageName}」单独指定模型。只影响这个任务、这一个阶段 —— 不改全局配置，也不影响别的任务。
              留空即回到全局默认。
            </p>

            <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
              <ModelSlot
                label="语言模型（本阶段）"
                value={ov.llm_model ?? ''}
                options={models?.llm.options ?? []}
                fallbackHint={`全局：${models?.llm.current ?? '-'}`}
                note={models?.llm.error ? `拉取列表失败：${models.llm.error.slice(0, 60)}` : models?.llm.note}
                disabled={busy != null}
                onSave={(v) => saveOverride({ llm_model: v })}
                onClear={() => saveOverride({ llm_model: '' })}
              />
              <ModelSlot
                label="生图模型（本阶段）"
                value={ov.image_model ?? ''}
                options={models?.image.options ?? []}
                fallbackHint={`全局：${models?.image.current ?? '-'} · 服务 ${models?.image.service || '-'}`}
                note={models?.image.note}
                disabled={busy != null}
                onSave={(v) => saveOverride({ image_model: v })}
                onClear={() => saveOverride({ image_model: '' })}
              />
              <ModelSlot
                label="配音音色（本阶段）"
                value={ov.tts_voice ?? ''}
                options={models?.tts.options ?? []}
                fallbackHint={`全局：${models?.tts.current ?? '-'}`}
                note={models?.tts.note}
                disabled={busy != null}
                onSave={(v) => saveOverride({ tts_voice: v })}
                onClear={() => saveOverride({ tts_voice: '' })}
              />
            </div>

            <div className="mt-3 rounded-lg border border-[#1e2839] bg-[#0b0f17] px-3 py-2.5">
              <div className="text-[10.5px] text-[#5f6d87] mb-1.5">给本阶段上传参考图（风格 / 角色参考）</div>
              <label className="inline-block text-[11.5px] px-3 py-1.5 rounded-lg bg-violet-500/15 hover:bg-violet-500/25 text-violet-300 cursor-pointer transition-colors">
                {busy === 'ref' ? '上传中…' : '选择图片'}
                <input
                  type="file"
                  accept="image/png,image/jpeg,image/webp"
                  className="hidden"
                  onChange={(e) => {
                    const f = e.target.files?.[0]
                    e.target.value = ''
                    if (!f) return
                    const r = new FileReader()
                    r.onload = async () => {
                      setBusy('ref')
                      try {
                        const res = await api.uploadStageRef(runId, stageId, { data_url: String(r.result) })
                        flash(`参考图已上传：${res.path}`)
                        onChanged()
                      } catch (er: any) {
                        setErr(er.message)
                      } finally {
                        setBusy(null)
                      }
                    }
                    r.readAsDataURL(f)
                  }}
                />
              </label>
              <span className="ml-2 text-[10.5px] text-[#4c5975]">存在 refs/{stageId}/ 下，重跑该阶段时可供模型参考</span>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

// ------------------------------------------------------------------ 小组件

const INPUT =
  'w-full text-[11px] bg-[#0f1420] border border-[#26324a] rounded-md px-2 py-1.5 text-[#dbe4f5] outline-none focus:border-[#4f6bff]/60'

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <div className="text-[10px] text-[#5f6d87] mb-1">{label}</div>
      {children}
    </div>
  )
}

function ModelSlot({
  label,
  value,
  options,
  fallbackHint,
  note,
  disabled,
  onSave,
  onClear,
}: {
  label: string
  value: string
  options: string[]
  fallbackHint: string
  note?: string
  disabled?: boolean
  onSave: (v: string) => void
  onClear: () => void
}) {
  const [draft, setDraft] = useState(value)
  const dirtyRef = useRef(false)
  useEffect(() => {
    if (!dirtyRef.current) setDraft(value)
  }, [value])

  return (
    <div className="rounded-lg border border-[#1e2839] bg-[#0b0f17] p-3">
      <div className="text-[11px] text-[#8b98b4] mb-1.5">{label}</div>
      <input
        value={draft}
        list={`opts-${label}`}
        placeholder="留空 = 用全局"
        disabled={disabled}
        onChange={(e) => {
          dirtyRef.current = true
          setDraft(e.target.value)
        }}
        className={INPUT}
      />
      <datalist id={`opts-${label}`}>
        {options.map((o) => (
          <option key={o} value={o} />
        ))}
      </datalist>
      <div className="text-[10px] text-[#4c5975] mt-1.5">{fallbackHint}</div>
      {note && <div className="text-[10px] text-[#4c5975] mt-0.5">{note}</div>}
      <div className="flex gap-2 mt-2">
        <button
          disabled={disabled}
          onClick={() => {
            dirtyRef.current = false
            onSave(draft)
          }}
          className="text-[11px] px-2.5 py-1 rounded-md bg-[#1a2233] hover:bg-[#24304a] text-[#c9d5ea] disabled:opacity-40"
        >
          应用
        </button>
        {value && (
          <button
            disabled={disabled}
            onClick={() => {
              dirtyRef.current = false
              setDraft('')
              onClear()
            }}
            className="text-[11px] px-2.5 py-1 rounded-md text-[#6f7d99] hover:text-[#a8b6d1] disabled:opacity-40"
          >
            恢复全局
          </button>
        )}
        {value && <span className="text-[10px] text-emerald-300 self-center">已指定</span>}
      </div>
    </div>
  )
}
