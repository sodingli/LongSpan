import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../lib/api'
import type { RunTree, TreeFile } from '../lib/types'
import { fmtSize } from '../lib/format'

/**
 * 任务目录表：这个任务到底产出了哪些文件、每个是谁写的、哪个是成片真正用的。
 *
 * 三条数据原则（和后端 file_index.py 一致）：
 * 1. 文件大小/时间取自**文件系统实测**，不是数据库里的过期登记值；
 * 2. 「谁写的」只认**真实留痕**（人工编辑记录、生图索引），没有留痕就是模型生成；
 * 3. 阶段名与顺序来自技能包 manifest，**不硬编码** —— 换个技能包，分组自动变。
 */

interface Props {
  runId: string
  onOpenFile: (rel: string) => void
  /** 只显示某一阶段的文件（画布节点里用） */
  stageFilter?: string
  compact?: boolean
}

const KIND_ICON: Record<string, string> = {
  markdown: '📄', text: '📃', json: '🧾', yaml: '⚙️', table: '📊',
  image: '🖼️', video: '🎬', audio: '🔊',
  pptx: '📊', docx: '📘', xlsx: '📗', pdf: '📕', other: '📎',
}

const SOURCE_TONE: Record<string, string> = {
  model: 'bg-[#1a2233] text-[#8b98b4]',
  human_edit: 'bg-amber-500/15 text-amber-300',
  human_upload: 'bg-amber-500/15 text-amber-300',
  human_redo: 'bg-amber-500/15 text-amber-300',
  stale: 'bg-[#161d29] text-[#5f6d87]',
  backup: 'bg-violet-500/15 text-violet-300',
  system: 'bg-sky-500/12 text-sky-300',
  aux: 'bg-[#1a2233] text-[#6b7994]',
}

const COL = 'grid grid-cols-[18px_minmax(0,1fr)_58px_68px_88px_86px] gap-2.5 items-center'

export default function DirectoryTable({ runId, onOpenFile, stageFilter, compact }: Props) {
  const [tree, setTree] = useState<RunTree | null>(null)
  const [err, setErr] = useState('')
  const [groupBy, setGroupBy] = useState<'stage' | 'dir'>('stage')
  const [q, setQ] = useState('')
  const [kindFilter, setKindFilter] = useState('')
  const [humanOnly, setHumanOnly] = useState(false)
  const [hideStale, setHideStale] = useState(true)
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const [exporting, setExporting] = useState(false)
  const [flash, setFlash] = useState('')

  const load = useCallback(async () => {
    try {
      setTree(await api.runTree(runId))
      setErr('')
    } catch (e) {
      setErr(String(e))
    }
  }, [runId])

  useEffect(() => {
    void load()
  }, [load])

  // 阶段顺序取自后端（= manifest 顺序），过滤之后仍按这个顺序排
  const orderOf = useMemo(() => {
    const g = groupBy === 'stage' ? tree?.groups_by_stage : tree?.groups_by_dir
    const m = new Map<string, { order: number; label: string }>()
    ;(g ?? []).forEach((it, i) => m.set(it.id, { order: i, label: it.label }))
    return m
  }, [tree, groupBy])

  const scopeFiles = useMemo(() => {
    if (!tree) return []
    // 注意用 !== undefined：画布上的「其他文件」节点传的是空串，
    // 它要的是"未归属阶段的那批文件"，不是"全部文件"。
    return stageFilter === undefined ? tree.files : tree.files.filter((f) => f.stage_id === stageFilter)
  }, [tree, stageFilter])

  const staleHidden = useMemo(
    () => (hideStale ? scopeFiles.filter((f) => f.source === 'stale').length : 0),
    [scopeFiles, hideStale],
  )

  const groups = useMemo(() => {
    const kw = q.trim().toLowerCase()
    let fs = scopeFiles
    if (hideStale) fs = fs.filter((f) => f.source !== 'stale')
    if (humanOnly) fs = fs.filter((f) => f.source.startsWith('human'))
    if (kindFilter) fs = fs.filter((f) => f.kind === kindFilter)
    if (kw) fs = fs.filter((f) => f.path.toLowerCase().includes(kw))

    const keyOf = (f: TreeFile) => (groupBy === 'stage' ? f.stage_id || '_unassigned' : f.dir || '(根目录)')
    const buckets = new Map<string, TreeFile[]>()
    for (const f of fs) {
      const k = keyOf(f)
      const arr = buckets.get(k)
      if (arr) arr.push(f)
      else buckets.set(k, [f])
    }
    return [...buckets.entries()]
      .map(([id, files]) => ({
        id,
        label: id === '_unassigned' ? '其他文件' : orderOf.get(id)?.label ?? id,
        order: orderOf.get(id)?.order ?? 999,
        bytes: files.reduce((a, b) => a + b.size, 0),
        files: files.sort((a, b) => a.path.localeCompare(b.path)),
      }))
      .sort((a, b) => a.order - b.order)
  }, [scopeFiles, groupBy, q, kindFilter, humanOnly, hideStale, orderOf])

  const shown = groups.reduce((a, g) => a + g.files.length, 0)

  async function doExport() {
    setExporting(true)
    setFlash('')
    try {
      const r = await api.exportTree(runId, groupBy)
      const blob = new Blob([r.markdown], { type: 'text/markdown;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `任务目录表-${runId.slice(0, 8)}.md`
      a.click()
      URL.revokeObjectURL(url)
      setFlash(`已生成 ${r.path}（${(r.bytes / 1024).toFixed(1)} KB）并下载；该文件也随「打包下载 zip」一起带走`)
    } catch (e) {
      setFlash(`导出失败：${String(e).slice(0, 120)}`)
    } finally {
      setExporting(false)
    }
  }

  if (err) {
    return (
      <div className="rounded-xl border border-rose-500/30 bg-rose-500/8 px-4 py-3 text-[12px] text-rose-200">
        目录表加载失败：{err.slice(0, 160)}
      </div>
    )
  }

  if (!tree) {
    return <div className="shimmer h-[120px] rounded-xl border border-[#1e2839]" />
  }

  const st = tree.stats
  const kinds = st.kinds.slice(0, 6)

  return (
    <div className="rounded-xl border border-[#1e2839] bg-[#0d1320] overflow-hidden">
      {/* 统计条 */}
      <div className="flex flex-wrap items-center gap-x-5 gap-y-2 px-4 py-3 border-b border-[#1e2839]">
        <Stat label="文件" value={String(st.files)} />
        <Stat label="总体积" value={fmtSize(st.bytes)} />
        {stageFilter === undefined && <Stat label="阶段" value={`${st.stages}/${st.stages + (st.unassigned ? 1 : 0)}`} />}
        <Stat label="目录" value={String(st.dirs)} />
        <Stat label="人工改动" value={String(st.human_touched)} tone={st.human_touched ? 'text-amber-300' : ''} />
        {st.active > 0 && <Stat label="本次启用" value={String(st.active)} tone="text-emerald-300" />}
        <div className="ml-auto flex items-center gap-2">
          <div className="flex rounded-lg overflow-hidden border border-[#1e2839]">
            {([['stage', '按阶段'], ['dir', '按目录']] as const).map(([k, label]) => (
              <button
                key={k}
                onClick={() => setGroupBy(k)}
                className={`px-2.5 py-1 text-[11px] transition-colors ${
                  groupBy === k ? 'bg-[#24304a] text-[#e6edfa]' : 'bg-[#131a28] text-[#7c8aa5] hover:text-[#c9d5ea]'
                }`}
              >
                {label}
              </button>
            ))}
          </div>
          <button
            disabled={exporting}
            onClick={() => void doExport()}
            className="text-[11px] px-2.5 py-1 rounded-lg bg-[#1a2233] hover:bg-[#24304a] text-[#c9d5ea] transition-colors disabled:opacity-50"
          >
            {exporting ? '生成中…' : '导出清单'}
          </button>
        </div>
      </div>

      {/* 筛选栏 */}
      <div className="flex flex-wrap items-center gap-2 px-4 py-2.5 border-b border-[#151d2b] bg-[#0b111c]">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="搜索文件名或路径…"
          className="w-[190px] bg-[#131a28] border border-[#1e2839] rounded-lg px-2.5 py-1 text-[11.5px] text-[#c9d5ea] placeholder:text-[#4d5a72] outline-none focus:border-[#4f6bff]/60"
        />
        {kinds.map((k) => (
          <button
            key={k.kind}
            onClick={() => setKindFilter(kindFilter === k.kind ? '' : k.kind)}
            className={`text-[10.5px] px-2 py-[3px] rounded-md border transition-colors ${
              kindFilter === k.kind
                ? 'bg-[#24304a] border-[#3b4a6b] text-[#e6edfa]'
                : 'bg-transparent border-[#1e2839] text-[#7c8aa5] hover:text-[#c9d5ea]'
            }`}
          >
            {KIND_ICON[k.kind] ?? '📎'} {k.label} {k.count}
          </button>
        ))}
        <label className="flex items-center gap-1.5 text-[10.5px] text-[#7c8aa5] cursor-pointer select-none">
          <input
            type="checkbox"
            checked={humanOnly}
            onChange={(e) => setHumanOnly(e.target.checked)}
            className="accent-[#4f6bff] w-3 h-3"
          />
          只看人工改动
        </label>
        <label className="flex items-center gap-1.5 text-[10.5px] text-[#7c8aa5] cursor-pointer select-none">
          <input
            type="checkbox"
            checked={hideStale}
            onChange={(e) => setHideStale(e.target.checked)}
            className="accent-[#4f6bff] w-3 h-3"
          />
          隐藏历史残留
          {staleHidden > 0 && <span className="text-[#5f6d87]">（已隐藏 {staleHidden} 个）</span>}
        </label>
        <span className="ml-auto text-[10.5px] text-[#5f6d87]">
          显示 {shown} / {scopeFiles.length}
        </span>
      </div>

      {/* 表头 */}
      <div className={`${COL} px-4 py-2 text-[10.5px] text-[#5f6d87] border-b border-[#151d2b]`}>
        <span />
        <span>文件</span>
        <span>类型</span>
        <span className="text-right">大小</span>
        <span>来源</span>
        <span className="text-right">修改时间</span>
      </div>

      <div className={compact ? 'max-h-[280px] overflow-auto' : 'max-h-[560px] overflow-auto'}>
        {groups.length === 0 && (
          <div className="px-4 py-6 text-[11.5px] text-[#5f6d87] text-center">没有匹配的文件。</div>
        )}
        {groups.map((g) => {
          const open = !collapsed.has(g.id)
          return (
            <div key={g.id}>
              <button
                onClick={() =>
                  setCollapsed((prev) => {
                    const next = new Set(prev)
                    if (next.has(g.id)) next.delete(g.id)
                    else next.add(g.id)
                    return next
                  })
                }
                className="w-full flex items-center gap-2 px-4 py-2 bg-[#111827] hover:bg-[#151d2b] transition-colors text-left border-b border-[#151d2b]"
              >
                <span className="text-[9px] text-[#5f6d87] w-2.5">{open ? '▾' : '▸'}</span>
                <span className="text-[11.5px] text-[#c9d5ea] font-medium">{g.label}</span>
                <span className="text-[10.5px] text-[#5f6d87]">
                  {g.files.length} 个 · {fmtSize(g.bytes)}
                </span>
              </button>
              {open &&
                g.files.map((f) => <Row key={f.path} f={f} runId={runId} onOpenFile={onOpenFile} />)}
            </div>
          )
        })}
      </div>

      <div className="px-4 py-2.5 border-t border-[#151e2b] bg-[#0b111c] flex items-center gap-3">
        <span className="text-[10.5px] text-[#5f6d87]">
          数据取自工作区实际文件；「模型生成 / 人工修改 / 人工上传 / 人工重画 / 历史残留」来自编辑留痕，不是推测。
        </span>
        {flash && <span className="text-[10.5px] text-emerald-300/90 ml-auto">{flash}</span>}
      </div>
    </div>
  )
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="flex items-baseline gap-1.5">
      <span className="text-[10.5px] text-[#5f6d87]">{label}</span>
      <span className={`text-[13px] font-medium ${tone || 'text-[#e6edfa]'}`}>{value}</span>
    </div>
  )
}

function Row({ f, runId, onOpenFile }: { f: TreeFile; runId: string; onOpenFile: (rel: string) => void }) {
  const dir = f.dir ? `${f.dir}/` : ''
  return (
    <div className={`${COL} px-4 py-[7px] border-b border-[#131a26] last:border-0 hover:bg-[#121826] group`}>
      <span className="text-[12px]">{KIND_ICON[f.kind] ?? '📎'}</span>
      <button onClick={() => onOpenFile(f.path)} className="min-w-0 text-left" title={f.note || f.path}>
        <span className="text-[11.5px] font-mono text-[#c9d5ea] truncate block">
          {dir && <span className="text-[#5f6d87]">{dir}</span>}
          {f.name}
        </span>
        {f.note && <span className="text-[10px] text-[#5f6d87] truncate block">{f.note}</span>}
      </button>
      <span className="text-[10.5px] text-[#7c8aa5]">
        {f.in_use && <span className="text-emerald-400/80 mr-1" title="本次成片实际用到">●</span>}
        {f.kind}
      </span>
      <span className="text-[10.5px] text-[#7c8aa5] text-right font-mono">{fmtSize(f.size)}</span>
      <span>
        <span className={`text-[9.5px] px-1.5 py-[2px] rounded ${SOURCE_TONE[f.source] ?? SOURCE_TONE.aux}`}>
          {f.source_label}
        </span>
      </span>
      <span className="flex items-center justify-end gap-1.5">
        <span className="text-[10px] text-[#5f6d87] font-mono group-hover:hidden">{f.mtime.slice(5, 16).replace('T', ' ')}</span>
        <a
          href={api.fileUrl(runId, f.path)}
          download
          onClick={(e) => e.stopPropagation()}
          className="hidden group-hover:inline text-[10px] px-1.5 py-[1px] rounded bg-[#1a2233] hover:bg-[#24304a] text-[#c9d5ea] transition-colors"
        >
          下载
        </a>
      </span>
    </div>
  )
}
