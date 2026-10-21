/**
 * 极简 Markdown 渲染（无第三方依赖）。
 * 只覆盖产物文档里真实会用到的语法：标题、粗体、斜体、行内码、代码块、
 * 列表、引用、表格、分隔线。够用即可，不追求完整规范。
 */

function esc(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

function inline(s: string): string {
  return esc(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
}

export function renderMarkdown(src: string): string {
  if (!src) return ''
  const lines = src.split('\n')
  const out: string[] = []
  let i = 0
  let inCode = false
  let codeBuf: string[] = []
  let listType: 'ul' | 'ol' | null = null

  const closeList = () => {
    if (listType) {
      out.push(`</${listType}>`)
      listType = null
    }
  }

  while (i < lines.length) {
    const line = lines[i]

    // 代码块
    if (line.trim().startsWith('```')) {
      if (inCode) {
        out.push(`<pre><code>${esc(codeBuf.join('\n'))}</code></pre>`)
        codeBuf = []
        inCode = false
      } else {
        closeList()
        inCode = true
      }
      i++
      continue
    }
    if (inCode) {
      codeBuf.push(line)
      i++
      continue
    }

    // 表格
    if (line.includes('|') && i + 1 < lines.length && /^\s*\|?[\s:|-]+\|/.test(lines[i + 1])) {
      closeList()
      const header = line.split('|').map((c) => c.trim()).filter((c, idx, arr) => !(idx === 0 && !c) && !(idx === arr.length - 1 && !c))
      i += 2
      const rows: string[][] = []
      while (i < lines.length && lines[i].includes('|')) {
        const cells = lines[i].split('|').map((c) => c.trim())
        if (cells.length && cells[0] === '') cells.shift()
        if (cells.length && cells[cells.length - 1] === '') cells.pop()
        rows.push(cells)
        i++
      }
      out.push(
        `<table><thead><tr>${header.map((h) => `<th>${inline(h)}</th>`).join('')}</tr></thead><tbody>` +
          rows.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join('')}</tr>`).join('') +
          '</tbody></table>',
      )
      continue
    }

    // 标题
    const h = /^(#{1,6})\s+(.*)$/.exec(line)
    if (h) {
      closeList()
      const lv = Math.min(h[1].length, 4)
      out.push(`<h${lv}>${inline(h[2])}</h${lv}>`)
      i++
      continue
    }

    // 分隔线
    if (/^\s*(---|\*\*\*)\s*$/.test(line)) {
      closeList()
      out.push('<hr style="border:none;border-top:1px solid #1e2839;margin:14px 0" />')
      i++
      continue
    }

    // 引用
    if (/^\s*>\s?/.test(line)) {
      closeList()
      out.push(`<blockquote>${inline(line.replace(/^\s*>\s?/, ''))}</blockquote>`)
      i++
      continue
    }

    // 列表
    const ul = /^\s*[-*]\s+(.*)$/.exec(line)
    const ol = /^\s*(\d+)\.\s+(.*)$/.exec(line)
    if (ul || ol) {
      const want: 'ul' | 'ol' = ul ? 'ul' : 'ol'
      if (listType !== want) {
        closeList()
        out.push(`<${want}>`)
        listType = want
      }
      out.push(`<li>${inline((ul ? ul[1] : ol![2]) || '')}</li>`)
      i++
      continue
    }

    // 空行
    if (!line.trim()) {
      closeList()
      i++
      continue
    }

    closeList()
    out.push(`<p>${inline(line)}</p>`)
    i++
  }

  closeList()
  if (inCode && codeBuf.length) out.push(`<pre><code>${esc(codeBuf.join('\n'))}</code></pre>`)
  return out.join('')
}
