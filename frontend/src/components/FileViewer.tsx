import { useCallback, useEffect, useState } from 'react'
import { api } from '../lib/api'
import { renderMarkdown } from '../lib/md'
import type { FileContent } from '../lib/types'

/**
 * 产物查看器 —— 同时也是文本产物的编辑入口。
 *
 * 为什么不另做一个"编辑器页"：创作者是从目录表 / 产物卡片点进来的，
 * 这时候心里想的就是「这句话我要改一下」。先关掉、再进画布、再切到 files 页签、
 * 再从列表里找一遍，是把一个三秒的动作拆成四步。
 *
 * 边界（与后端一致）：
 * - 文本类（md / json / txt / yaml / csv / srt）可改，「提交」即落盘；
 * - 系统记账文件（media/images.json 等）只读，后端 API 层也会拒；
 * - 图片 / 视频 / 音频不走文本编辑，这里只做预览。
 */

interface Props {
  runId: string
  relPath: string
  onClose: () => void
  /** 提交/还原后通知外层刷新产物列表（大小、改过标记） */
  onChanged?: () => void
}

const TEXT_EXT_RE = /\.(md|markdown|json|txt|ya?ml|csv|srt)$/i

export default function FileViewer({ runId, relPath, onClose, onChanged }: Props) {
  const [content, setContent] = useState('')
  const [original, setOriginal] = useState('')
  const [meta, setMeta] = useState<FileContent | null>(null)
  const [loading, setLoading] = useState(true)
  const [editing, setEditing] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)

  const isImage = /\.(svg|png|jpe?g|webp)$/i.test(relPath)
  const isJson = /\.json$/i.test(relPath)
  const isMarkdown = /\.(md|markdown)$/i.test(relPath)
  const isVideo = /\.(mp4|webm)$/i.test(relPath)
  const isAudio = /\.(mp3|m4a|wav|aac|ogg)$/i.test(relPath)
  const isMedia = isVideo || isAudio
  const isText = TEXT_EXT_RE.test(relPath)

  const locked = !!meta?.system
  const canEdit = isText && !locked
  const dirty = canEdit && content !== original

  const flash = (t: string) => {
    setMsg(t)
    window.setTimeout(() => setMsg((m) => (m === t ? null : m)), 4000)
  }

  // 文本统一走 /artifact/content：既拿原文，也拿到「能不能改」的权威判断
  useEffect(() => {
    if (!isText) {
      setLoading(false)
      return
    }
    let alive = true
    setLoading(true)
    setErr(null)
    setEditing(false)
    api
      .fileContent(runId, relPath)
      .then((r) => {
        if (!alive) return
        setMeta(r)
        setContent(r.text)
        setOriginal(r.text)
      })
      .catch((e: any) => alive && setErr(e.message))
      .finally(() => alive && setLoading(false))
    return () => {
      alive = false
    }
  }, [runId, relPath, isText])

  const submit = useCallback(async () => {
    if (!canEdit || busy) return
    if (!dirty) {
      flash('内容没有改动')
      return
    }
    setBusy(true)
    setErr(null)
    try {
      const r = await api.saveFileContent(runId, { path: relPath, content })
      setOriginal(content)
      if (r.changed) {
        // 只更新"有没有备份"这一位；bytes 是字节数，别拿字符数糊上去
        setMeta((m) => (m ? { ...m, has_origin: m.has_origin || !!r.backed_up_origin } : m))
        flash(`已提交保存（${r.chars} 字${r.backed_up_origin ? '，模型原件已备份' : ''}）`)
        onChanged?.()
      } else {
        flash(r.note || '内容没有变化')
      }
    } catch (e: any) {
      setErr(e.message)
    } finally {
      setBusy(false)
    }
  }, [canEdit, busy, dirty, relPath, runId, content, onChanged])

  const restore = async () => {
    setBusy(true)
    setErr(null)
    try {
      await api.restoreStageFile(runId, { path: relPath })
      const r = await api.fileContent(runId, relPath)
      setMeta(r)
      setContent(r.text)
      setOriginal(r.text)
      flash('已用模型原件覆盖')
      onChanged?.()
    } catch (e: any) {
      setErr(e.message)
    } finally {
      setBusy(false)
    }
  }

  const requestClose = () => {
    if (dirty && !window.confirm('有未提交的改动，确定关闭？')) return
    onClose()
  }

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') {
        e.preventDefault()
        void submit()
        return
      }
      if (e.key === 'Escape') requestClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [submit, dirty, onClose])

  return (
    <div
      className="fixed inset-0 z-50 bg-black/70 backdrop-blur-sm flex items-center justify-center p-8"
      onClick={requestClose}
    >
      <div
        className="bg-[#0f1420] border border-[#1e2839] rounded-2xl w-full max-w-[980px] max-h-full flex flex-col overflow-hidden shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* 头部 */}
        <div className="px-5 py-3.5 border-b border-[#1e2839] flex items-center gap-2 flex-wrap">
          <span className="text-[13px] font-mono text-[#c9d5ea] truncate">{relPath}</span>
          {locked && (
            <span className="text-[9.5px] px-1.5 py-[1px] rounded bg-amber-500/15 text-amber-300 shrink-0">
              系统文件 · 只读
            </span>
          )}
          {canEdit && meta?.has_origin && (
            <span className="text-[9.5px] px-1.5 py-[1px] rounded bg-violet-500/15 text-violet-300 shrink-0">
              人工改过
            </span>
          )}

          <div className="ml-auto flex items-center gap-2 shrink-0">
            {canEdit && (
              <div className="flex rounded-lg overflow-hidden border border-[#26324a]">
                <button
                  onClick={() => setEditing(false)}
                  className={`text-[11px] px-2.5 py-1 transition-colors ${
                    editing ? 'text-[#7c8aa5] hover:text-[#c9d5ea]' : 'bg-[#1d2738] text-[#dbe4f5]'
                  }`}
                >
                  预览
                </button>
                <button
                  onClick={() => setEditing(true)}
                  className={`text-[11px] px-2.5 py-1 transition-colors ${
                    editing ? 'bg-[#1d2738] text-[#dbe4f5]' : 'text-[#7c8aa5] hover:text-[#c9d5ea]'
                  }`}
                >
                  编辑
                </button>
              </div>
            )}
            <a
              href={api.fileUrl(runId, relPath)}
              download
              className="text-[11px] px-2.5 py-1 rounded-lg bg-[#1a2233] hover:bg-[#24304a] text-[#c9d5ea] transition-colors"
            >
              下载
            </a>
            <button
              onClick={requestClose}
              className="w-7 h-7 rounded-lg hover:bg-[#1a2233] text-[#7c8aa5] hover:text-white transition-colors"
            >
              ✕
            </button>
          </div>
        </div>

        {(err || msg) && (
          <div className="px-5 pt-3 space-y-1.5">
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

        {/* 内容 */}
        <div className="flex-1 overflow-auto min-h-0">
          {loading ? (
            <div className="p-8 text-[12px] text-[#5f6d87]">读取中…</div>
          ) : isImage ? (
            <div className="p-6 flex justify-center bg-[#080b12]">
              <img
                src={api.fileUrl(runId, relPath)}
                alt={relPath}
                className="max-h-[70vh] rounded-xl border border-[#1e2839]"
              />
            </div>
          ) : isVideo ? (
            <div className="p-6 flex justify-center bg-[#080b12]">
              <video
                src={api.fileUrl(runId, relPath)}
                controls
                autoPlay
                playsInline
                className="max-h-[70vh] rounded-xl border border-[#1e2839] bg-black"
              />
            </div>
          ) : isAudio ? (
            <div className="p-8 flex flex-col items-center gap-4 bg-[#080b12]">
              <div className="text-[12px] text-[#7c8aa5] font-mono">{relPath.split('/').pop()}</div>
              <audio src={api.fileUrl(runId, relPath)} controls className="w-full max-w-[560px]" />
            </div>
          ) : editing && canEdit ? (
            <div className="p-4">
              <textarea
                value={content}
                onChange={(e) => setContent(e.target.value)}
                spellCheck={false}
                autoFocus
                className="w-full h-[58vh] text-[12.5px] leading-relaxed bg-[#0b0f17] border border-[#26324a] rounded-lg px-3.5 py-3 text-[#dbe4f5] outline-none focus:border-[#4f6bff]/60 resize-none"
                style={{ fontFamily: 'var(--font-mono, ui-monospace, monospace)' }}
              />
              <div className="flex items-center gap-2 mt-2.5 flex-wrap">
                <button
                  onClick={submit}
                  disabled={busy || !dirty}
                  className="text-[12px] px-3.5 py-1.5 rounded-lg bg-[#4f6bff] hover:bg-[#3f5ae8] text-white font-medium disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  {busy ? '提交中…' : '提交保存'}
                </button>
                <button
                  onClick={() => setContent(original)}
                  disabled={busy || !dirty}
                  className="text-[11.5px] px-3 py-1.5 rounded-lg text-[#6f7d99] hover:text-[#a8b6d1] disabled:opacity-40"
                >
                  放弃改动
                </button>
                {meta?.has_origin && (
                  <button
                    onClick={restore}
                    disabled={busy}
                    className="text-[11.5px] px-3 py-1.5 rounded-lg bg-[#1a2233] hover:bg-[#24304a] text-[#c9d5ea] disabled:opacity-40"
                  >
                    还原模型原件
                  </button>
                )}
                {dirty && <span className="text-[10.5px] text-orange-300">有未提交改动</span>}
                <span className="ml-auto text-[10px] text-[#4c5975]">
                  ⌘/Ctrl + S 提交{isJson ? ' · 保存时会校验 JSON 语法' : ''}
                </span>
              </div>
            </div>
          ) : isMarkdown ? (
            <div
              className="md-body px-6 py-5 text-[13px] text-[#c9d5ea]"
              dangerouslySetInnerHTML={{ __html: renderMarkdown(content) }}
            />
          ) : isJson ? (
            <pre className="px-6 py-5 text-[12px] text-[#9fb0cc] font-mono whitespace-pre-wrap break-words leading-relaxed">
              {prettyJson(content)}
            </pre>
          ) : (
            <pre className="px-6 py-5 text-[12px] text-[#9fb0cc] font-mono whitespace-pre-wrap break-words leading-relaxed">
              {content}
            </pre>
          )}
        </div>
      </div>
    </div>
  )
}

/** 预览时把 JSON 排整齐；编辑时用的是原文，不会被悄悄重排 */
function prettyJson(text: string): string {
  try {
    return JSON.stringify(JSON.parse(text), null, 2)
  } catch {
    return text
  }
}
