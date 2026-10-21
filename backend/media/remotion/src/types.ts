/** 分镜合成器的数据契约 —— Python 侧 `app/media/compose.py` 生成同构的 props.json。 */

/** 一格分镜 */
export type Panel = {
  /** 画面：相对 public 目录的路径（由 Python 侧用 --public-dir 指到本次任务的工作目录） */
  src: string
  /**
   * 多帧画面：首帧 / 中间帧 / 尾帧，按顺序播放，帧间交叉过渡。
   * 只有一张时就是静态图 + 运镜（老行为）。缺省时退回 [src]。
   */
  frames?: string[]
  /** 视频片段：存在时**优先播放视频**，静态帧只作为兜底 */
  video?: string | null
  /** 该格在时间轴上的起始秒（由 Python 侧统一排时间轴，Remotion 只负责渲染） */
  fromSeconds: number
  /** 该格时长（秒） */
  durationInSeconds: number
  /** 镜头运动（决策 D12：甲方案 = Remotion 运镜） */
  motion: Motion
  /** 字幕正文（台词/旁白） */
  subtitle?: string
  /** 说话人（字幕前缀，可空） */
  speaker?: string
  /** 格号标注，如 "格 1" */
  label?: string
  /** 是否为此格画面是占位图（真实生图不可用时的降级，必须在画面上标注） */
  isPlaceholder?: boolean
}

/** 音轨片段（配音） */
export type AudioClip = {
  src: string
  /** 起始秒 */
  from: number
  /** 持续秒（不填则按音频自然长度） */
  durationInSeconds?: number
  volume?: number
  /** 说话人，仅用于调试显示 */
  speaker?: string
}

export type Motion =
  | 'zoom-in'
  | 'zoom-out'
  | 'pan-left'
  | 'pan-right'
  | 'pan-up'
  | 'pan-down'
  | 'static'

export type ComicDramaProps = {
  title: string
  panels: Panel[]
  audio: AudioClip[]
  bgm?: { src: string; volume: number } | null
  fps: number
  width: number
  height: number
  /** 全片总时长（秒）——由 Python 侧算好，Remotion 不猜 */
  durationInSeconds: number
  /** 片头卡 */
  intro?: { durationInSeconds: number; title: string; subtitle?: string } | null
  /** 片尾卡（空则不生成） */
  outro?: { durationInSeconds: number; text: string; hint?: string } | null
  /** 是否显示水印 */
  showWatermark?: boolean
}
