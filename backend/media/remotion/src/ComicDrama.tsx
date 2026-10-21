import {
  AbsoluteFill,
  Audio,
  interpolate,
  Sequence,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from 'remotion'
import { Panel } from './Panel'
import type { ComicDramaProps } from './types'

/**
 * 成片主组件。
 *
 * 分工（关键设计）：**时间轴由 Python 侧算好**（`app/media/compose.py`），
 * 这里只负责"给定 from/duration 就把它画出来"。
 * 为什么这样切：时间轴涉及配音时长核算、格时长对齐这类业务判断，
 * 属于技能/流程逻辑；Remotion 工程应当是纯粹的渲染器（换技能不用改这里）。
 */

const CN_FONT = '"PingFang SC","Hiragino Sans GB","Heiti SC","Microsoft YaHei",sans-serif'

const Centered: React.FC<{
  children: React.ReactNode
  durationInFrames: number
  background: string
}> = ({ children, durationInFrames, background }) => {
  const frame = useCurrentFrame()
  const { fps } = useVideoConfig()
  const fade = Math.round(fps * 0.3)
  const end = Math.max(1, durationInFrames - 1)
  const opacity = interpolate(frame, [0, fade, end - fade, end], [0, 1, 1, 0], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  })
  return (
    <AbsoluteFill
      style={{
        background,
        opacity,
        alignItems: 'center',
        justifyContent: 'center',
        display: 'flex',
      }}
    >
      {children}
    </AbsoluteFill>
  )
}

export const ComicDrama: React.FC<ComicDramaProps> = ({
  title,
  panels,
  audio,
  bgm,
  fps,
  width,
  intro,
  outro,
  showWatermark = true,
}) => {
  const baseFont = Math.round(width * 0.05)

  return (
    <AbsoluteFill style={{ backgroundColor: '#080b13' }}>
      {/* ---------------- 音轨 ---------------- */}
      {audio.map((clip, i) => (
        <Sequence
          key={`audio-${i}`}
          from={Math.round(clip.from * fps)}
          durationInFrames={
            clip.durationInSeconds ? Math.max(1, Math.round(clip.durationInSeconds * fps)) : undefined
          }
        >
          <Audio src={staticFile(clip.src)} volume={clip.volume ?? 1} />
        </Sequence>
      ))}
      {bgm ? <Audio src={staticFile(bgm.src)} volume={bgm.volume} loop /> : null}

      {/* ---------------- 片头 ---------------- */}
      {intro ? (
        <Sequence from={0} durationInFrames={Math.round(intro.durationInSeconds * fps)}>
          <Centered
            durationInFrames={Math.round(intro.durationInSeconds * fps)}
            background="linear-gradient(160deg,#141c33 0%,#080b13 70%)"
          >
            <div style={{ textAlign: 'center', padding: `0 ${width * 0.1}px` }}>
              <div
                style={{
                  color: '#fff',
                  fontFamily: CN_FONT,
                  fontSize: Math.round(baseFont * 1.28),
                  fontWeight: 800,
                  lineHeight: 1.35,
                  textShadow: '0 6px 26px rgba(0,0,0,0.75)',
                }}
              >
                {intro.title}
              </div>
              {intro.subtitle ? (
                <div
                  style={{
                    color: '#9fb0cc',
                    fontFamily: CN_FONT,
                    fontSize: Math.round(baseFont * 0.52),
                    marginTop: 22,
                    letterSpacing: 3,
                  }}
                >
                  {intro.subtitle}
                </div>
              ) : null}
            </div>
          </Centered>
        </Sequence>
      ) : null}

      {/* ---------------- 分镜 ---------------- */}
      {panels.map((p, i) => {
        const durFrames = Math.max(1, Math.round(p.durationInSeconds * fps))
        return (
          <Sequence
            key={`panel-${i}`}
            from={Math.round(p.fromSeconds * fps)}
            durationInFrames={durFrames}
          >
            <Panel
              src={p.src}
              frames={p.frames}
              video={p.video}
              motion={p.motion}
              durationInFrames={durFrames}
              subtitle={p.subtitle}
              speaker={p.speaker}
              label={p.label}
              isPlaceholder={p.isPlaceholder}
            />
          </Sequence>
        )
      })}

      {/* ---------------- 片尾 ---------------- */}
      {outro ? (
        <Sequence
          from={Math.round(outroStartSeconds(panels, intro) * fps)}
          durationInFrames={Math.round(outro.durationInSeconds * fps)}
        >
          <Centered
            durationInFrames={Math.round(outro.durationInSeconds * fps)}
            background="linear-gradient(0deg,#141c33 0%,#080b13 70%)"
          >
            <div style={{ textAlign: 'center', padding: `0 ${width * 0.12}px` }}>
              <div
                style={{
                  color: '#fff',
                  fontFamily: CN_FONT,
                  fontSize: Math.round(baseFont * 0.92),
                  fontWeight: 700,
                  lineHeight: 1.5,
                }}
              >
                {outro.text}
              </div>
              {outro.hint ? (
                <div
                  style={{
                    color: '#7c8aa5',
                    fontFamily: CN_FONT,
                    fontSize: Math.round(baseFont * 0.44),
                    marginTop: 18,
                  }}
                >
                  {outro.hint}
                </div>
              ) : null}
            </div>
          </Centered>
        </Sequence>
      ) : null}

      {/* ---------------- 水印 ---------------- */}
      {showWatermark ? (
        <div
          style={{
            position: 'absolute',
            right: width * 0.045,
            bottom: width * 0.045,
            color: 'rgba(255,255,255,0.34)',
            fontFamily: CN_FONT,
            fontSize: Math.round(baseFont * 0.4),
            letterSpacing: 1,
          }}
        >
          {title}
        </div>
      ) : null}
    </AbsoluteFill>
  )
}

/** 片尾的起始秒：片头 + 所有分镜之后 */
function outroStartSeconds(
  panels: ComicDramaProps['panels'],
  intro: ComicDramaProps['intro']
): number {
  const introDur = intro ? intro.durationInSeconds : 0
  return panels.reduce((max, p) => Math.max(max, p.fromSeconds + p.durationInSeconds), introDur)
}
