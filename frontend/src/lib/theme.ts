import { useEffect, useState } from 'react'

export type ThemeMode = 'auto' | 'light' | 'dark'

const KEY = 'longspan-theme'

export function getStoredTheme(): ThemeMode {
  try {
    const v = localStorage.getItem(KEY)
    return v === 'light' || v === 'dark' ? v : 'auto'
  } catch {
    return 'auto'
  }
}

function applyTheme(mode: ThemeMode) {
  if (mode === 'auto') delete document.documentElement.dataset.theme
  else document.documentElement.dataset.theme = mode
}

export function setTheme(mode: ThemeMode) {
  try {
    if (mode === 'auto') localStorage.removeItem(KEY)
    else localStorage.setItem(KEY, mode)
  } catch {
    /* localStorage 不可用时仅影响记忆，不影响切换 */
  }
  applyTheme(mode)
}

/** 主题切换钩子：初始值与 index.html 里的首帧脚本保持一致 */
export function useTheme() {
  const [mode, setMode] = useState<ThemeMode>(getStoredTheme)
  // 首帧脚本已应用过主题；这里兜底一次（例如 localStorage 在首帧后被解锁的场景）
  useEffect(() => applyTheme(mode), [])
  const change = (m: ThemeMode) => {
    setTheme(m)
    setMode(m)
  }
  return { mode, setMode: change }
}
