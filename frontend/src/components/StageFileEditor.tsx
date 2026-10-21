import { useCallback, useEffect, useState } from 'react'
import { api } from '../lib/api'
import type { StageFile, StageFiles } from '../lib/types'
import { fmtSize } from '../lib/format'

/**
 * 阶段产物编辑器 —— 让「每个步骤都能改」真的成立。
 *
 * 之前只有分镜能改（那是画布最早的需求）。但真实使用里，人最先想改的往往是别的阶段：
 * 约束理解偏了、剧本某句台词太蠢、角色锚点写歪了。要求用户"重跑整个阶段"只为改一句话，
 * 既慢又贵，还可能顺手把别的产物改坏。
 *
 * 三条边界（与后端一致，不在这里重新发明）：
 * 1. 文本类（md / json / txt…）可以直接编辑；JSON 保存时会被后端做语法校验；
 * 2. 二进制（图 / 视频 / 音频）只能整份替换，替换后标注「人工上传」，不冒充模型产物；
 * 3. 系统记账文件（media/images.json 等）不给入口 —— 改了会让幂等复用与渲染对不上账。
 */

interface Props {
  runId: string
  stageId: string
  onChanged: () => void
}

const KIND_ICON: Record<string, string> = {
  markdown: '📄',
  text: '📄',
  json: '🧾',
  yaml: '🧾',
  table: '📊',
  image: '🖼',
  video: '🎬',
  audio: '🎧',
}

/** 阶段级修改建议（模型只评审、不动产物） */
interface StageSug {
  overall: string
  suggestions: { target: string; issue: string; suggestion: string; priority: string }[]
  model: string
}
const PRIORITY_CLS: Record<string, string> = {
  high: 'bg-rose-500/15 text-rose-300',
  medium: 'bg-amber-500/15 text-amber-300',
  low: 'bg-[#151d2b] text-[#8b98b4]',
}

export default function StageFileEditor({ runId, stageId, onChanged }: Props) {
  const [data, setData] = useState<StageFiles | null>(null)
  const [active, setActive] = useState<StageFile | null>(null)
  const [text, setText] = useState('')
  const [original, setOriginal] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const [sug, setSug] = useState<StageSug | null>(null)
  const [sugNote, setSugNote] = useState('')
  const [sugOpen, setSugOpen] = useState(false)

  const flash = (t: string) => {
    setMsg(t)
    window.setTimeout(() => setMsg((m) => (m === t ? null : m)), 4000)
  }

  const load = useCallback(async () => {
    setErr(null)
    try {
      const d = await api.stageFiles(runId, stageId)
      setData(d)
      setSug(null) // 换阶段后上一阶段的建议不再有意义
      setActive((cur) => {
        if (cur && d.files.some((f) => f.path === cur.path)) return d.files.find((f) => f.path === cur.path) ?? null
        return d.files.find((f) => f.editable) ?? d.files[0] ?? null
      })
    } catch (e: any) {
      setErr(e.message)
    }
  }, [runId, stageId])

  useEffect(() => {
    void load()
  }, [load])

  // 选中文件变化 → 拉文本内容（二进制不拉）
  useEffect(() => {
    if (!active) {
      setText('')
      setOriginal('')
      return
    }
    if (!active.editable) {
      setText('')
      setOriginal('')
      return
    }
    let alive = true
    api
      .fileContent(runId, active.path)
      .then((r) => {
        if (!alive) return
        setText(r.text)
        setOriginal(r.text)
      })
      .catch((e: any) => alive && setErr(e.message))
    return () => {
      alive = false
    }
  }, [active?.path, active?.editable, runId])

  const save = async () => {
    if (!active) return
    setBusy('save')
    setErr(null)
    try {
      const r = await api.saveFileContent(runId, { path: active.path, content: text, stage_id: stageId })
      flash(r.changed ? `已保存 ${active.path}（${r.chars} 字，模型原件已备份）` : r.note || '内容没有变化')
      setOriginal(text)
      await load()
      onChanged()
    } catch (e: any) {
      setErr(e.message)
    } finally {
      setBusy(null)
    }
  }

  const restore = async () => {
    if (!active) return
    setBusy('restore')
    setErr(null)
    try {
      await api.restoreStageFile(runId, { path: active.path, stage_id: stageId })
      flash(`已用备份原件还原 ${active.path}`)
      await load()
      if (active.editable) {
        const r = await api.fileContent(runId, active.path)
        setText(r.text)
        setOriginal(r.text)
      }
      onChanged()
    } catch (e: any) {
      setErr(e.message)
    } finally {
      setBusy(null)
    }
  }

  const replace = (f: File) => {
    if (!active) return
    const reader = new FileReader()
    reader.onload = async () => {
      setBusy('upload')
      setErr(null)
      try {
        const r = await api.uploadStageFile(runId, {
          path: active.path,
          data_url: String(reader.result),
          stage_id: stageId,
          note: `人工上传替换 ${active.path}`,
        })
        flash(`已替换 ${active.path}（${fmtSize(r.bytes)}）`)
        await load()
        onChanged()
      } catch (e: any) {
        setErr(e.message)
      } finally {
        setBusy(null)
      }
    }
    reader.readAsDataURL(f)
  }

  const dirty = active?.editable && text !== original

  // ---------------- 阶段级 AI 修改建议（只读）----------------
  const askSuggestion = async () => {
    setBusy('sug')
    setErr(null)
    try {
      const r = await api.suggestStage(runId, stageId, { note: sugNote })
      setSug(r)
      setSugOpen(true)
    } catch (e: any) {
      setErr(e.message)
    } finally {
      setBusy(null)
    }
  }

  // 与弹窗查看器保持一致：⌘/Ctrl+S 直接提交
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') {
        e.preventDefault()
        if (dirty && busy == null) void save()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  return (
    <div>
      <div className="flex items-center gap-2 mb-2 flex-wrap">
        <span className="text-[11px] text-[#6f7d99]">
          本阶段共 {data?.count ?? 0} 个产物 · 可编辑 {data?.editable_count ?? 0} · 可替换媒体{' '}
          {data?.replaceable_count ?? 0}
        </span>
        <button
          disabled={busy != null}
          onClick={askSuggestion}
          title="让大模型评审本阶段产物，给出具体修改建议（只读，不改任何文件）"
          className="ml-auto text-[11px] px-2.5 py-1 rounded-md bg-teal-500/15 hover:bg-teal-500/25 text-teal-300 disabled:opacity-40"
        >
          {busy === 'sug' ? '评审中…' : 'AI 修改建议'}
        </button>
        <button
          onClick={() => void load()}
          className="text-[11px] px-2.5 py-1 rounded-md bg-[#151d2b] hover:bg-[#1d2738] text-[#9fb0cc]"
        >
          刷新
        </button>
      </div>

      {/* AI 建议卡 */}
      {sugOpen && sug && (
        <div className="mb-2 rounded-lg border border-teal-500/25 bg-teal-500/[0.04] p-3">
          <div className="flex items-center gap-2 mb-1.5 flex-wrap">
            <span className="text-[11px] font-medium text-teal-300">AI 总评</span>
            <span className="text-[9.5px] text-[#5f6d87]">{sug.model}</span>
            <button
              onClick={() => setSugOpen(false)}
              className="ml-auto text-[10.5px] text-[#6f7d99] hover:text-rose-300"
            >
              收起
            </button>
          </div>
          <p className="text-[11px] leading-relaxed text-[#c9d5ea]">{sug.overall || '（模型没有给总评）'}</p>
          <div className="mt-2 space-y-1.5">
            {sug.suggestions.map((s, i) => (
              <div key={i} className="rounded-md bg-[#0f1420] border border-[#1e2839] px-2.5 py-2">
                <div className="flex items-center gap-1.5 mb-1 flex-wrap">
                  <span className={`text-[9px] px-1.5 py-[1px] rounded ${PRIORITY_CLS[s.priority] ?? PRIORITY_CLS.low}`}>
                    {s.priority}
                  </span>
                  <span className="text-[10px] text-[#8b98b4] font-mono truncate max-w-[60%]" title={s.target}>
                    {s.target}
                  </span>
                </div>
                <div className="text-[10.5px] leading-relaxed text-[#c9d5ea]">
                  <span className="text-rose-300/80">问题：</span>
                  {s.issue}
                </div>
                <div className="text-[10.5px] leading-relaxed text-[#c9d5ea] mt-0.5">
                  <span className="text-teal-300/80">建议：</span>
                  {s.suggestion}
                </div>
              </div>
            ))}
            {!sug.suggestions.length && <div className="text-[10.5px] text-[#5f6d87]">模型没有提出具体建议。</div>}
          </div>
          <div className="flex items-center gap-2 mt-2 flex-wrap">
            <input
              value={sugNote}
              onChange={(e) => setSugNote(e.target.value)}
              placeholder="告诉它重点看哪里，例如：结尾钩子不够狠"
              className="flex-1 min-w-[200px] text-[10.5px] bg-[#0f1420] border border-[#26324a] rounded-lg px-2 py-1 text-[#dbe4f5] outline-none focus:border-teal-500/50"
            />
            <button
              disabled={busy != null}
              onClick={askSuggestion}
              className="text-[10.5px] px-2.5 py-1 rounded-lg bg-[#1a2233] hover:bg-[#24304a] text-[#c9d5ea] disabled:opacity-40"
            >
              {busy === 'sug' ? '…' : '按此要求重新评审'}
            </button>
          </div>
          <div className="text-[9.5px] text-[#4c5975] mt-1.5">
            建议是只读的 —— 照着改请编辑左侧对应文件，改完「提交保存」，模型原件会自动备份。
          </div>
        </div>
      )}

      {(err || msg) && (
        <div className="mb-2 space-y-1.5">
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

      <div className="grid grid-cols-[220px_1fr] gap-3">
        {/* 文件列表 */}
        <div className="rounded-lg border border-[#1e2839] bg-[#0b0f17] max-h-[420px] overflow-y-auto">
          {(data?.files ?? []).length === 0 && (
            <div className="px-3 py-3 text-[11px] text-[#5f6d87]">本阶段还没有产物文件。</div>
          )}
          {(data?.files ?? []).map((f) => (
            <button
              key={f.path}
              onClick={() => setActive(f)}
              className={`w-full text-left px-2.5 py-2 border-b border-[#141b28] last:border-b-0 transition-colors ${
                active?.path === f.path ? 'bg-[#151d2b]' : 'hover:bg-[#111726]'
              }`}
            >
              <div className="flex items-center gap-1.5">
                <span className="text-[11px]">{KIND_ICON[f.kind] ?? '📄'}</span>
                <span className="text-[11px] text-[#c9d5ea] truncate flex-1" title={f.path}>
                  {f.name}
                </span>
                {f.system && <span className="text-[9px] text-[#4c5975]">系统</span>}
                {f.has_origin && <span className="text-[9px] text-violet-300">改过</span>}
              </div>
              <div className="text-[9.5px] text-[#4c5975] mt-0.5 flex items-center gap-1.5">
                <span>{fmtSize(f.size)}</span>
                {f.dir && <span className="truncate">{f.dir}/</span>}
                {f.in_use && <span className="text-emerald-400/80">在用</span>}
              </div>
            </button>
          ))}
        </div>

        {/* 编辑区 */}
        <div className="rounded-lg border border-[#1e2839] bg-[#0b0f17] p-3 min-h-[260px]">
          {!active ? (
            <div className="text-[11.5px] text-[#5f6d87]">从左侧选择一个文件。</div>
          ) : (
            <>
              <div className="flex items-center gap-2 flex-wrap mb-2">
                <span className="text-[12px] text-[#dbe4f5] font-mono">{active.path}</span>
                <span className="text-[10px] text-[#5f6d87]">{fmtSize(active.size)}</span>
                {active.source_label && (
                  <span className="text-[9.5px] px-1.5 py-[1px] rounded bg-[#151d2b] text-[#8b98b4]">
                    {active.source_label}
                  </span>
                )}
              </div>

              {active.system ? (
                <div className="text-[11.5px] text-amber-200/90 leading-relaxed">
                  这是系统记账文件（工具写入的索引/渲染参数），不提供直接编辑 ——
                  改它会让「复用已完成素材」失效，或让成片与实际素材对不上。
                  要改画面请去「分镜提示词」页，要换图请用上传替换。
                </div>
              ) : active.editable ? (
                <>
                  <textarea
                    value={text}
                    onChange={(e) => setText(e.target.value)}
                    rows={16}
                    spellCheck={false}
                    className="w-full text-[11.5px] leading-relaxed bg-[#0f1420] border border-[#26324a] rounded-lg px-3 py-2.5 text-[#dbe4f5] outline-none focus:border-[#4f6bff]/60 resize-y"
                    style={{ fontFamily: 'var(--font-mono, ui-monospace, monospace)' }}
                  />
                  <div className="flex items-center gap-2 mt-2 flex-wrap">
                    <button
                      disabled={busy != null || !dirty}
                      onClick={save}
                      className="text-[11.5px] px-3 py-1.5 rounded-lg bg-[#4f6bff] hover:bg-[#3f5ae8] text-white font-medium disabled:opacity-40 disabled:cursor-not-allowed"
                    >
                      {busy === 'save' ? '提交中…' : '提交保存'}
                    </button>
                    <button
                      disabled={busy != null || !dirty}
                      onClick={() => setText(original)}
                      className="text-[11.5px] px-3 py-1.5 rounded-lg text-[#6f7d99] hover:text-[#a8b6d1] disabled:opacity-40"
                    >
                      放弃改动
                    </button>
                    <button
                      disabled={busy != null || !active.has_origin}
                      onClick={restore}
                      title={active.has_origin ? '用首次人工编辑前的模型原件覆盖' : '还没被改过，没有备份原件'}
                      className="text-[11.5px] px-3 py-1.5 rounded-lg bg-[#1a2233] hover:bg-[#24304a] text-[#c9d5ea] disabled:opacity-40"
                    >
                      {busy === 'restore' ? '还原中…' : '还原模型原件'}
                    </button>
                    {dirty && <span className="text-[10.5px] text-orange-300">有未保存改动</span>}
                    {active.ext === '.json' && (
                      <span className="text-[10px] text-[#4c5975]">保存时会校验 JSON 语法</span>
                    )}
                  </div>
                </>
              ) : active.replaceable ? (
                <div>
                  <div className="text-[11px] text-[#6f7d99] mb-2">
                    二进制产物不走文本编辑 —— 只能整份替换。替换后来源会标成「人工上传」，不冒充模型产物。
                  </div>
                  {active.kind === 'image' && (
                    <img
                      src={api.fileUrl(runId, active.path)}
                      alt={active.name}
                      className="max-h-[220px] rounded-lg border border-[#1e2839] mb-2"
                    />
                  )}
                  {active.kind === 'video' && (
                    <video
                      src={api.fileUrl(runId, active.path)}
                      controls
                      className="max-h-[220px] rounded-lg border border-[#1e2839] mb-2"
                    />
                  )}
                  {active.kind === 'audio' && (
                    <audio src={api.fileUrl(runId, active.path)} controls className="w-full mb-2" />
                  )}
                  <div className="flex gap-2">
                    <label className="text-[11.5px] px-3 py-1.5 rounded-lg bg-violet-500/15 hover:bg-violet-500/25 text-violet-300 cursor-pointer">
                      {busy === 'upload' ? '上传中…' : '上传替换'}
                      <input
                        type="file"
                        accept={
                          active.kind === 'image'
                            ? 'image/png,image/jpeg,image/webp'
                            : active.kind === 'video'
                              ? 'video/mp4,video/quicktime,video/webm'
                              : 'audio/mpeg,audio/mp4,audio/wav'
                        }
                        className="hidden"
                        onChange={(e) => {
                          const f = e.target.files?.[0]
                          if (f) replace(f)
                          e.target.value = ''
                        }}
                      />
                    </label>
                    <button
                      disabled={busy != null || !active.has_origin}
                      onClick={restore}
                      className="text-[11.5px] px-3 py-1.5 rounded-lg bg-[#1a2233] hover:bg-[#24304a] text-[#c9d5ea] disabled:opacity-40"
                    >
                      还原备份
                    </button>
                  </div>
                </div>
              ) : (
                <div className="text-[11.5px] text-[#5f6d87]">
                  这类文件不在可编辑范围内（{active.ext || '无扩展名'}）。需要改它请重跑对应阶段。
                </div>
              )}
            </>
          )}
        </div>
      </div>

      {!!data?.stage.missing?.length && (
        <div className="mt-2 text-[10.5px] text-amber-300/80">
          本阶段声明但还没产出：{data.stage.missing.join('、')}（说明该阶段未跑完，或产物被删了）
        </div>
      )}
    </div>
  )
}
