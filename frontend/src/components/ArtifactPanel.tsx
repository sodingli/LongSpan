import type { Artifact } from '../lib/types'
import { api } from '../lib/api'

interface Props {
  runId: string
  artifacts: Artifact[]
  files: string[]
  onOpenFile: (rel: string) => void
}

export default function ArtifactPanel({ runId, artifacts, files, onOpenFile }: Props) {
  // DB 登记的可能不全（例如早期阶段），用文件系统列表兜底
  const merged: { rel_path: string; kind: string; size: number; is_draft: boolean }[] = []
  const seen = new Set<string>()
  for (const a of artifacts) {
    merged.push({ rel_path: a.rel_path, kind: a.kind, size: a.size, is_draft: a.is_draft })
    seen.add(a.rel_path)
  }
  for (const f of files) {
    if (!seen.has(f)) merged.push({ rel_path: f, kind: guessKind(f), size: 0, is_draft: false })
  }

  const images = merged.filter((m) => m.kind === 'image')
  const videos = merged.filter((m) => m.kind === 'video')
  const audios = merged.filter((m) => m.kind === 'audio')

  return (
    <section>
      <div className="flex items-center gap-2 mb-3">
        <h2 className="text-[13.5px] font-medium">产物 · {merged.length}</h2>
        {merged.length > 0 && (
          <a
            href={api.downloadUrl(runId)}
            className="ml-auto text-[11px] px-2.5 py-1 rounded-lg bg-[#1a2233] hover:bg-[#24304a] text-[#c9d5ea] transition-colors"
          >
            打包下载 zip
          </a>
        )}
      </div>

      {merged.length === 0 && (
        <div className="text-[11.5px] text-[#5f6d87] px-1 py-3">还没有产物，跑起来就会出现。</div>
      )}

      {/* 成片：竖屏播放器（这是用户真正想要的那个东西，放最上面） */}
      {videos.map((v) => (
        <div key={v.rel_path} className="mb-3 rounded-xl border border-[#1e2839] bg-[#0d1320] p-3">
          <div className="flex items-center gap-2 mb-2">
            <span className="text-[11px] px-1.5 py-[2px] rounded bg-emerald-500/15 text-emerald-300">成片</span>
            <span className="text-[11px] text-[#c9d5ea] font-mono truncate">{v.rel_path}</span>
            <a
              href={api.fileUrl(runId, v.rel_path)}
              download
              className="ml-auto text-[11px] px-2 py-0.5 rounded-md bg-[#1a2233] hover:bg-[#24304a] text-[#c9d5ea] transition-colors shrink-0"
            >
              下载
            </a>
          </div>
          <div className="flex justify-center">
            <video
              src={api.fileUrl(runId, v.rel_path)}
              controls
              playsInline
              preload="metadata"
              className="max-h-[52vh] rounded-lg border border-[#1e2839] bg-black"
            />
          </div>
        </div>
      ))}

      {/* 配音音轨 */}
      {audios.length > 0 && (
        <div className="mb-3 rounded-xl border border-[#1e2839] bg-[#0d1320] p-3">
          <div className="text-[11px] text-[#7c8aa5] mb-2">配音 · {audios.length} 段</div>
          <div className="flex flex-col gap-1.5 max-h-[180px] overflow-auto">
            {audios.map((a) => (
              <div key={a.rel_path} className="flex items-center gap-2">
                <span className="text-[10.5px] font-mono text-[#7c8aa5] w-[104px] shrink-0 truncate">
                  {a.rel_path.split('/').pop()}
                </span>
                <audio src={api.fileUrl(runId, a.rel_path)} controls className="h-7 flex-1" />
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 分镜缩略图墙 */}
      {images.length > 0 && (
        <div className="grid grid-cols-4 gap-2 mb-3">
          {images.map((m) => (
            <button
              key={m.rel_path}
              onClick={() => onOpenFile(m.rel_path)}
              className="group relative rounded-lg overflow-hidden border border-[#1e2839] hover:border-[#4f6bff]/60 bg-[#0d1320] aspect-[9/16] transition-colors"
            >
              <img src={api.fileUrl(runId, m.rel_path)} alt={m.rel_path} className="w-full h-full object-cover" />
              <div className="absolute inset-x-0 bottom-0 px-1.5 py-1 bg-black/70 text-[9.5px] text-[#c9d5ea] font-mono truncate opacity-0 group-hover:opacity-100 transition-opacity">
                {m.rel_path.split('/').pop()}
              </div>
            </button>
          ))}
        </div>
      )}

      {/* 文件清单不在这里平铺 —— 「任务目录表」会按阶段/目录分层列出全部文件，
          并标出每个文件是谁写的。这里只负责"把成品拿出来看"。 */}
    </section>
  )
}

function guessKind(path: string): string {
  const ext = path.split('.').pop()?.toLowerCase() ?? ''
  if (['md', 'markdown'].includes(ext)) return 'markdown'
  if (ext === 'json') return 'json'
  if (['svg', 'png', 'jpg', 'jpeg', 'webp'].includes(ext)) return 'image'
  if (['mp3', 'wav', 'm4a', 'aac'].includes(ext)) return 'audio'
  if (['mp4', 'webm'].includes(ext)) return 'video'
  if (ext === 'pptx') return 'pptx'
  if (ext === 'docx') return 'docx'
  return 'text'
}
