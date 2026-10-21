import { AbsoluteFill, Img, OffthreadVideo, interpolate, staticFile, useCurrentFrame, useVideoConfig } from 'remotion'
import type { Motion } from './types'

/**
 * 单格画面。
 *
 * 这里承担「动态漫」里"动"的部分，优先级从高到低：
 *   1. 有视频片段 → 直接播视频（真运动，模型生成的）；
 *   2. 有多帧     → 帧间交叉过渡 + 每帧各自运镜（首→中→尾，动作的"起承转"）；
 *   3. 只有一帧   → 静态图 + 运镜（Ken Burns，最省的兜底）。
 *
 * 为什么多帧要用"交叉过渡"而不是硬切：三帧本来是同一动作的三个时刻，
 * 硬切会让观众以为换了镜头；叠化才能读成"连续动作"。
 */

/** 每种运镜的起止参数：画面统一先放大到 1.06 以上，避免平移时露黑边 */
const MOTION_TABLE: Record<Motion, { scale: [number, number]; x: [number, number]; y: [number, number] }> = {
  'zoom-in': { scale: [1.06, 1.2], x: [0, 0], y: [0, 0] },
  'zoom-out': { scale: [1.2, 1.06], x: [0, 0], y: [0, 0] },
  'pan-left': { scale: [1.14, 1.14], x: [30, -30], y: [0, 0] },
  'pan-right': { scale: [1.14, 1.14], x: [-30, 30], y: [0, 0] },
  'pan-up': { scale: [1.14, 1.14], x: [0, 0], y: [26, -26] },
  'pan-down': { scale: [1.14, 1.14], x: [0, 0], y: [-26, 26] },
  static: { scale: [1.02, 1.02], x: [0, 0], y: [0, 0] },
}

/** 多帧时每帧的运镜：交替推/拉，避免"三次一模一样的推近" */
const CYCLE: Motion[] = ['zoom-in', 'pan-right', 'zoom-out', 'pan-left']

const imgStyle: React.CSSProperties = { width: '100%', height: '100%', objectFit: 'cover' }

export const Panel: React.FC<{
  src: string
  frames?: string[]
  video?: string | null
  motion: Motion
  durationInFrames: number
  subtitle?: string
  speaker?: string
  label?: string
  isPlaceholder?: boolean
}> = ({ src, frames, video, motion, durationInFrames, subtitle, speaker, label, isPlaceholder }) => {
  const frame = useCurrentFrame()
  const { fps, width } = useVideoConfig()
  const end = Math.max(1, durationInFrames - 1)

  // 画面来源：视频 > 多帧 > 单帧
  const sources = (frames && frames.length ? frames : [src]).filter(Boolean)
  const useVideo = Boolean(video)
  const segments = useVideo ? 1 : Math.max(1, sources.length)
  const segLen = Math.max(1, Math.floor(durationInFrames / segments))
  // 交叉过渡时长：0.35s，但不能长到把短片段整个吃掉
  const xfade = Math.max(2, Math.min(Math.round(fps * 0.35), Math.floor(segLen / 3)))

  // 入画 / 出画淡入淡出（各 0.22s）—— 让格与格之间的切换不生硬
  const fadeFrames = Math.min(Math.round(fps * 0.22), Math.floor(durationInFrames / 3))
  const opacity = interpolate(
    frame,
    [0, fadeFrames, end - fadeFrames, end],
    [0, 1, 1, 0],
    { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' }
  )

  // 字幕：等入画结束后再出现，避免和转场撞在一起
  const subDelay = Math.min(Math.round(fps * 0.35), Math.floor(durationInFrames / 2))
  const subOpacity = interpolate(frame, [subDelay, subDelay + Math.round(fps * 0.25)], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  })
  const subShift = interpolate(frame, [subDelay, subDelay + Math.round(fps * 0.25)], [18, 0], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  })

  const baseFont = Math.round(width * 0.046)

  return (
    <AbsoluteFill style={{ backgroundColor: '#000', opacity }}>
      {useVideo ? (
        <AbsoluteFill>
          {/* muted 是刻意的：这一格的对白由配音轨负责（时序已经排好），
              视频片段自带的环境音会和它对冲，听起来像两拨人在说话。 */}
          <OffthreadVideo src={staticFile(video as string)} style={imgStyle} muted />
        </AbsoluteFill>
      ) : (
        sources.map((s, i) => {
          const start = i * segLen
          const stop = i === sources.length - 1 ? durationInFrames : start + segLen
          const local = frame - start
          const localEnd = Math.max(1, stop - start - 1)
          const m = MOTION_TABLE[(i === 0 ? motion : CYCLE[i % CYCLE.length]) as Motion] ?? MOTION_TABLE['zoom-in']
          const scale = interpolate(local, [0, localEnd], m.scale, {
            extrapolateLeft: 'clamp',
            extrapolateRight: 'clamp',
          })
          const x = interpolate(local, [0, localEnd], m.x, { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' })
          const y = interpolate(local, [0, localEnd], m.y, { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' })

          // 段内透明度：段间交叠 xfade 帧做叠化；首段不淡入（整格入画已处理），
          // 末段不淡出（整格出画已处理）。
          // 注意：interpolate 要求 inputRange 严格递增 —— 首段如果也带 xfade，
          // 会得到 [0,0,...] 直接抛 "inputRange must be strictly monotonically increasing"（已踩过）。
          const isFirst = i === 0
          const isLast = i === sources.length - 1
          let segOpacity = 1
          if (sources.length > 1) {
            if (isFirst) {
              const outStart = Math.max(1, stop - xfade)
              segOpacity = interpolate(frame, [0, outStart, Math.max(outStart + 1, stop)], [1, 1, isLast ? 1 : 0], {
                extrapolateLeft: 'clamp',
                extrapolateRight: 'clamp',
              })
            } else if (isLast) {
              segOpacity = interpolate(frame, [start, start + xfade, Math.max(start + xfade + 1, stop)], [0, 1, 1], {
                extrapolateLeft: 'clamp',
                extrapolateRight: 'clamp',
              })
            } else {
              const outStart = Math.max(start + xfade + 1, stop - xfade)
              segOpacity = interpolate(frame, [start, start + xfade, outStart, Math.max(outStart + 1, stop)], [0, 1, 1, 0], {
                extrapolateLeft: 'clamp',
                extrapolateRight: 'clamp',
              })
            }
          }

          return (
            <AbsoluteFill
              key={`${s}-${i}`}
              style={{
                opacity: segOpacity,
                transform: `scale(${scale}) translate(${x}px, ${y}px)`,
                transformOrigin: 'center center',
              }}
            >
              <Img src={staticFile(s)} style={imgStyle} />
            </AbsoluteFill>
          )
        })
      )}

      {/* 上下压暗，保证字幕在任何画面上都读得清 */}
      <AbsoluteFill
        style={{
          background:
            'linear-gradient(to bottom, rgba(0,0,0,0.34) 0%, rgba(0,0,0,0) 22%, rgba(0,0,0,0) 62%, rgba(0,0,0,0.62) 100%)',
        }}
      />

      {/* 格号（左上，弱化） */}
      {label ? (
        <div
          style={{
            position: 'absolute',
            top: width * 0.055,
            left: width * 0.055,
            color: 'rgba(255,255,255,0.72)',
            fontFamily: '"PingFang SC","Hiragino Sans GB","Heiti SC",sans-serif',
            fontSize: Math.round(baseFont * 0.52),
            letterSpacing: 2,
            textShadow: '0 2px 8px rgba(0,0,0,0.8)',
          }}
        >
          {label}
        </div>
      ) : null}

      {/* 占位图必须画面上标注 —— 禁止静默降级 */}
      {isPlaceholder ? (
        <div
          style={{
            position: 'absolute',
            top: width * 0.055,
            right: width * 0.055,
            color: 'rgba(255,214,120,0.92)',
            border: '2px solid rgba(255,214,120,0.55)',
            borderRadius: 8,
            padding: '4px 10px',
            fontFamily: '"PingFang SC","Hiragino Sans GB","Heiti SC",sans-serif',
            fontSize: Math.round(baseFont * 0.44),
          }}
        >
          占位图 · 未接生图
        </div>
      ) : null}

      {/* 字幕（底部安全区） */}
      {subtitle ? (
        <div
          style={{
            position: 'absolute',
            left: width * 0.08,
            right: width * 0.08,
            bottom: width * 0.16,
            opacity: subOpacity,
            transform: `translateY(${subShift}px)`,
          }}
        >
          {speaker ? (
            <span
              style={{
                color: '#ffd878',
                fontFamily: '"PingFang SC","Hiragino Sans GB","Heiti SC",sans-serif',
                fontSize: Math.round(baseFont * 0.62),
                fontWeight: 600,
                marginRight: 8,
                textShadow: '0 2px 10px rgba(0,0,0,0.95)',
              }}
            >
              {speaker}
            </span>
          ) : null}
          <span
            style={{
              color: '#fff',
              fontFamily: '"PingFang SC","Hiragino Sans GB","Heiti SC",sans-serif',
              fontSize: baseFont,
              fontWeight: 700,
              lineHeight: 1.42,
              textShadow: '0 3px 14px rgba(0,0,0,0.95), 0 1px 3px rgba(0,0,0,0.9)',
            }}
          >
            {subtitle}
          </span>
        </div>
      ) : null}
    </AbsoluteFill>
  )
}
