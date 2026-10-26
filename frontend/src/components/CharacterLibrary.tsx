import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../lib/api'
import type { CharacterSummary } from '../lib/types'

/**
 * 角色库：工作台级的资产页。
 *
 * 产品定位上一句话：**角色属于工作台，不属于某一集**。
 * 这里能看到所有定稿的角色、他们出自哪一集、被复用过几次；
 * 复用入口在创建任务时（CreatePanel），因为「用谁」是开跑前要决定的事。
 */
export default function CharacterLibrary({ onNotify }: { onNotify?: (msg: string) => void }) {
  const [items, setItems] = useState<CharacterSummary[]>([])
  const [active, setActive] = useState<CharacterSummary | null>(null)
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<string | null>(null)
  const [nameDraft, setNameDraft] = useState('')
  const fileRef = useRef<HTMLInputElement | null>(null)

  const load = useCallback(async () => {
    try {
      const d = await api.characters()
      setItems(d.characters)
    } catch (e: any) {
      setNote(`加载失败：${e.message}`)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    setNameDraft(active?.name ?? '')
  }, [active?.id])

  const scan = async () => {
    setBusy(true)
    try {
      const r = await api.scanCharacters()
      setNote(`已扫描 ${r.runs} 集：新增 ${r.registered} 个角色，既有 ${r.existing} 个`)
      await load()
    } catch (e: any) {
      setNote(e.message)
    } finally {
      setBusy(false)
    }
  }

  const saveName = async () => {
    if (!active || !nameDraft.trim() || nameDraft === active.name) return
    setBusy(true)
    try {
      const updated = await api.updateCharacter(active.id, { name: nameDraft.trim() })
      setActive(updated)
      await load()
      onNotify?.(`已改名为「${updated.name}」`)
    } catch (e: any) {
      setNote(e.message)
    } finally {
      setBusy(false)
    }
  }

  const uploadArt = async (file: File) => {
    if (!active) return
    setBusy(true)
    try {
      const dataUrl = await new Promise<string>((resolve, reject) => {
        const fr = new FileReader()
        fr.onload = () => resolve(String(fr.result))
        fr.onerror = () => reject(new Error('读文件失败'))
        fr.readAsDataURL(file)
      })
      const updated = await api.updateCharacter(active.id, { art_data_url: dataUrl })
      setActive(updated)
      await load()
      onNotify?.('定妆图已替换（后续复用都用这张）')
    } catch (e: any) {
      setNote(e.message)
    } finally {
      setBusy(false)
    }
  }

  const remove = async () => {
    if (!active || !confirm(`删除角色「${active.name}」？历史集里的产物副本不受影响。`)) return
    setBusy(true)
    try {
      await api.deleteCharacter(active.id)
      setActive(null)
      await load()
    } catch (e: any) {
      setNote(e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="max-w-[1080px] mx-auto px-8 py-8">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-[19px] font-semibold tracking-tight">角色库</h1>
          <p className="text-[12px] text-[#7c8aa5] mt-1.5 leading-relaxed">
            定稿的角色在这里沉淀。下一集选他，用的是<strong className="text-[#c9d5ea]">同一张定妆图</strong>
            —— 不是重新生成一张尽量像的。
          </p>
        </div>
        <button
          onClick={scan}
          disabled={busy}
          className="shrink-0 text-[11.5px] px-3 py-2 rounded-lg border border-[#1e2839] text-[#8b98b4] hover:border-[#4f6bff]/50 hover:text-[#c9d5ea] transition-colors disabled:opacity-40"
        >
          从历史任务导入
        </button>
      </div>

      {note && (
        <div className="mt-4 rounded-lg border border-[#1e2839] bg-[#0f1420] px-3.5 py-2.5 text-[11.5px] text-[#8b98b4]">
          {note}
          <button onClick={() => setNote(null)} className="ml-3 underline opacity-60">关闭</button>
        </div>
      )}

      {items.length === 0 ? (
        <div className="mt-16 text-center text-[12.5px] text-[#5f6d87] leading-relaxed">
          还没有角色。<br />
          跑完一集（精修通道含角色设计阶段），角色会自动入库；也可以点右上角「从历史任务导入」。
        </div>
      ) : (
        <div className="mt-6 grid grid-cols-[repeat(auto-fill,minmax(168px,1fr))] gap-3">
          {items.map((c) => (
            <button
              key={c.id}
              onClick={() => setActive(c)}
              className={`group text-left rounded-xl border overflow-hidden transition-all ${
                active?.id === c.id
                  ? 'border-[#4f6bff]/70 bg-[#121826]'
                  : 'border-[#1e2839] bg-[#0f1420] hover:border-[#4f6bff]/40'
              }`}
            >
              <div className="aspect-[4/5] bg-[#0a0e16] overflow-hidden">
                {c.has_art ? (
                  <img
                    src={api.characterArtUrl(c.id)}
                    alt={c.name}
                    className="w-full h-full object-cover group-hover:scale-[1.02] transition-transform"
                  />
                ) : (
                  <div className="w-full h-full flex items-center justify-center text-[10.5px] text-[#47536b]">
                    无定妆图
                  </div>
                )}
              </div>
              <div className="px-2.5 py-2">
                <div className="text-[12px] font-medium text-[#c9d5ea] truncate">{c.name}</div>
                <div className="text-[10.5px] text-[#5f6d87] mt-0.5">
                  出场 {c.appearances.length} 集 · 复用 {Math.max(0, c.usage_count - 1)} 次
                </div>
              </div>
            </button>
          ))}
        </div>
      )}

      {active && (
        <div className="mt-8 rounded-xl border border-[#1e2839] bg-[#0b0f17] p-4">
          <div className="flex gap-4">
            <div className="w-[132px] shrink-0">
              <div className="aspect-[4/5] rounded-lg overflow-hidden bg-[#0a0e16]">
                {active.has_art ? (
                  <img src={api.characterArtUrl(active.id)} alt={active.name} className="w-full h-full object-cover" />
                ) : null}
              </div>
              <input
                ref={fileRef}
                type="file"
                accept="image/*"
                className="hidden"
                onChange={(e) => {
                  const f = e.target.files?.[0]
                  if (f) void uploadArt(f)
                  e.target.value = ''
                }}
              />
              <button
                onClick={() => fileRef.current?.click()}
                disabled={busy}
                className="w-full mt-2 text-[10.5px] py-1.5 rounded-lg border border-[#1e2839] text-[#8b98b4] hover:border-[#4f6bff]/50 disabled:opacity-40"
              >
                换定妆图
              </button>
            </div>

            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2">
                <input
                  value={nameDraft}
                  onChange={(e) => setNameDraft(e.target.value)}
                  onBlur={saveName}
                  className="text-[14px] font-medium bg-transparent border-b border-transparent hover:border-[#1e2839] focus:border-[#4f6bff]/60 outline-none px-0.5 py-0.5 text-[#e6edf8]"
                />
                <span className="text-[10px] px-1.5 py-[2px] rounded bg-[#1a2233] text-[#6f7d99]">
                  {active.slug}
                </span>
              </div>
              <div className="text-[10.5px] text-[#5f6d87] mt-1">
                出自 {active.source_run_id.slice(0, 8)} · 出场 {active.appearances.length} 集 · 画风 {active.style || '—'}
              </div>

              <div className="mt-3">
                <div className="text-[10.5px] text-[#7c8aa5] mb-1">一致性锚点（复用时逐字带进每一格）</div>
                <div className="text-[11px] text-[#9aa8c2] leading-relaxed bg-[#0f1420] rounded-lg p-2.5 max-h-[132px] overflow-y-auto">
                  {active.anchor}
                </div>
              </div>

              <div className="flex items-center gap-3 mt-3">
                <span className="text-[10.5px] text-[#5f6d87]">复用方式：建任务时勾选，或在画布「角色」里应用</span>
                <button onClick={remove} disabled={busy} className="ml-auto text-[10.5px] text-rose-300/80 hover:text-rose-200 disabled:opacity-40">
                  删除角色
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
