import { Composition } from 'remotion'
import { ComicDrama } from './ComicDrama'
import type { ComicDramaProps } from './types'

/**
 * 一个技能 = 一个 Composition。
 *
 * 目前只有漫剧（ComicDrama）；M2 的 PPT 视频版、M3 的论文答辩版
 * 会各自加一个 Composition，互不干扰。
 */

const DEFAULT_PROPS: ComicDramaProps = {
  title: 'Longspan',
  panels: [],
  audio: [],
  bgm: null,
  fps: 30,
  width: 1080,
  height: 1920,
  durationInSeconds: 6,
  intro: null,
  outro: null,
  showWatermark: true,
}

export const RemotionRoot: React.FC = () => {
  return (
    <Composition
      id="ComicDrama"
      component={ComicDrama}
      durationInFrames={Math.max(1, Math.round(DEFAULT_PROPS.durationInSeconds * DEFAULT_PROPS.fps))}
      fps={DEFAULT_PROPS.fps}
      width={DEFAULT_PROPS.width}
      height={DEFAULT_PROPS.height}
      defaultProps={DEFAULT_PROPS}
      // 时长/分辨率由 Python 侧传入的 props 决定，这里不写死
      calculateMetadata={({ props }) => ({
        durationInFrames: Math.max(
          1,
          Math.round((props.durationInSeconds ?? DEFAULT_PROPS.durationInSeconds) * (props.fps ?? 30))
        ),
        fps: props.fps ?? 30,
        width: props.width ?? DEFAULT_PROPS.width,
        height: props.height ?? DEFAULT_PROPS.height,
      })}
    />
  )
}
