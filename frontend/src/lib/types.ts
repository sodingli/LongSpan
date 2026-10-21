export type RunStatus =
  | 'pending' | 'running' | 'awaiting_gate' | 'paused'
  | 'succeeded' | 'failed' | 'cancelled'
  /** 进程重启导致执行中断，可续跑 */
  | 'interrupted'

export interface SkillSummary {
  id: string
  name: string
  version: string
  description: string
  icon: string
  tags: string[]
  entry_inputs: EntryInput[]
  presets: { id: string; label: string; brief: Record<string, string> }[]
  outputs: string[]
  stage_count: number
}

export interface EntryInput {
  key: string
  label: string
  type: 'text' | 'textarea' | 'number' | 'select'
  required?: boolean
  default?: string | number
  placeholder?: string
  options?: string[]
}

export interface StageDef {
  id: string
  name: string
  icon: string
  description: string
  produces: string[]
  human_approval_default: boolean
  fast_channel: boolean
  success_criteria: string[]
  review_focus: string[]
}

export interface SkillDetail extends SkillSummary {
  stages: StageDef[]
  quota: Record<string, number>
  skill_md: string
  references: string[]
}

export interface RunSummary {
  id: string
  skill_id: string
  skill_name: string
  title: string
  status: RunStatus
  channel: string
  origin_channel: string
  ttfv_seconds: number | null
  resumed_count: number
  progress: number
  current_stage_id: string | null
  tokens_used: number
  cost_usd: number
  error: string | null
  created_at: string | null
}

/** 草稿诚实性元数据（后端 build_draft_info 产出） */
export interface DraftInfo {
  is_draft: boolean
  origin_channel: string
  ttfv_seconds: number | null
  target_seconds: number
  within_target: boolean | null
  gaps: string[]
  unrun_stages: { id: string; name: string; icon: string; reason: string }[]
  can_upgrade: boolean
  resume_count: number
}

export interface Step {
  id: string
  stage_id: string
  stage_name: string
  index: number
  is_sub_stage?: boolean
  parent_stage_id?: string | null
  status: string
  attempt: number
  summary: string
  detail: Record<string, any>
  /** 真实起止时间（画布时间轴的唯一依据，不许估算） */
  started_at?: string | null
  finished_at?: string | null
}

/** 阶段动作执行结果（runner 写入 Step.detail.actions） */
export interface StageAction {
  tool: string
  ok: boolean
  summary?: string
  error?: string | null
}

// ---- 画布编辑 ----

/** 一格分镜：模型产出的描述 + 人可改的字段 + 当前挂的图 */
export interface CanvasPanel {
  index: number
  prompt: string
  scene?: string
  motion?: string
  duration_seconds?: number
  subtitle?: string
  speaker?: string
  ratio?: string
  /** 给视频模型的提示词（留空则复用画面描述） */
  video_prompt?: string
  /** 每一帧可以单独写提示词（留空则从画面描述派生） */
  frame_prompts?: Record<string, string>
  /** 当前这一格挂的图（相对路径，空 = 还没出图） */
  image: string
  image_provider?: string
  image_source?: 'model' | 'upload' | 'placeholder'
  image_bytes?: number
  is_placeholder?: boolean
  /** 多帧：首 / 中 / 尾，没图的位置是 null */
  frames: Record<FrameRole, CanvasFrame | null>
  frame_count: number
  /** 已生成的视频片段（只有 status=ready 才会进成片） */
  video: CanvasVideo | null
  video_status?: string
  reference_video?: string
}

export type FrameRole = 'first' | 'mid' | 'last'

export interface CanvasFrame {
  path: string
  provider: string
  source: 'model' | 'upload' | 'placeholder'
  size: number
  is_placeholder: boolean
  note?: string
}

export interface CanvasVideo {
  status: 'running' | 'ready' | 'failed'
  path?: string
  error?: string
  duration?: number
  mode?: string
  model?: string
  task_id?: string
  started_at?: string
  finished_at?: string
  prompt?: string
  reference?: string
  frames?: string[]
  /** 被模型丢下的帧位（如 mid：首尾帧协议没有中间帧位置） */
  dropped_frames?: string[]
  note?: string
}

export interface CanvasCharacters {
  text: string
  has_origin: boolean
  origin_text: string
  roles: string[]
}

export interface CanvasVideoConfig {
  ready: boolean
  reason: string
  model: string
  models: string[]
  durations: number[]
  default_duration: number
  mode: string
}

export interface CanvasData {
  run_id: string
  skill_id: string
  panels: CanvasPanel[]
  panel_count: number
  panels_human_edited: boolean
  characters: CanvasCharacters
  /** stage_id -> { llm_model, image_model, image_service, tts_voice, tts_speed, video_model } */
  overrides: Record<string, Record<string, string>>
  edits: Record<string, number>
  frame_roles: { id: FrameRole; label: string }[]
  video: CanvasVideoConfig
}

export interface CanvasModels {
  llm: { current: string; base_url: string; options: string[]; error: string; note: string }
  image: { current: string; service: string; provider: string; options: string[]; note: string }
  tts: { current: string; options: string[]; note: string }
}

// ---- 阶段产物编辑（任何步骤都能改）----

export interface StageFile {
  path: string
  name: string
  dir: string
  ext: string
  kind: string
  size: number
  mtime: string
  source: string
  source_label: string
  note: string
  in_use: boolean
  /** 可以在文本框里改（md / json / txt …） */
  editable: boolean
  /** 可以整份替换（图片 / 视频 / 音频） */
  replaceable: boolean
  /** 系统记账文件，不给改 */
  system: boolean
  has_origin: boolean
}

export interface StageFiles {
  run_id: string
  stage: { id: string; name: string; produces: string[]; missing: string[] }
  files: StageFile[]
  count: number
  editable_count: number
  replaceable_count: number
  limits: {
    text_bytes: number
    image_bytes: number
    media_bytes: number
    text_ext: string[]
  }
}

export interface FileContent {
  path: string
  text: string
  bytes: number
  lines: number
  ext: string
  has_origin: boolean
  /** 系统记账文件（索引 / 渲染参数），只读 */
  system?: boolean
  editable?: boolean
}

// ---- 媒体配置（图片 / 语音 / 视频）----

export interface MediaKindState {
  kind: 'image' | 'tts' | 'video'
  values: Record<string, any> & { api_key_set?: boolean; api_key_masked?: string }
  providers: string[]
  ready: { ready: boolean; mode: string; hint: string }
  env_keys: Record<string, string>
}

export interface MediaConfigState {
  kinds: Record<string, MediaKindState>
  env_file: string
  env_file_exists: boolean
  notes: Record<string, string>
}

export interface MediaTestResult {
  ok: boolean
  stage?: string
  latency_ms?: number
  error?: string
  hint?: string
  message?: string
  preview?: string
  bytes?: number
  http_status?: number
  detail?: string
  model?: string
  download_url?: string
}

export interface Gate {
  id: string
  stage_id: string
  status: 'pending' | 'approved' | 'rejected' | 'revised'
  title: string
  prompt: string
  proposed: { files?: string[]; review_focus?: string[]; success_criteria?: string[] }
  decision: Record<string, any>
}

export interface Artifact {
  id: string
  stage_id: string | null
  kind: string
  name: string
  rel_path: string
  size: number
  is_draft: boolean
  is_sample: boolean
}

export interface Decision {
  id: string
  stage_id: string | null
  actor: 'model' | 'user'
  what: string
  why: string
  alternatives: { option?: string; confidence?: number; reason?: string }[]
  reversible: boolean
}

export interface RunEvent {
  id: number
  run_id: string
  type: string
  stage_id: string | null
  message: string
  payload: Record<string, any>
  at: string
}

export interface RunDetail extends RunSummary {
  steps: Step[]
  gates: Gate[]
  artifacts: Artifact[]
  decisions: Decision[]
  checkpoints: { id: string; stage_id: string; status: string; human_approved: boolean }[]
  files: string[]
  brief: Record<string, string>
  workspace: string
  draft: DraftInfo
}

// ---- 模型配置 ----

export interface ModelStatus {
  mode: 'real' | 'mock'
  provider: string
  configured_provider: string
  configured: boolean
  model: string
  base_url: string
  /** 备用模型链（主模型不可用时按顺序顶上） */
  fallback_models?: string[]
  fallback_raw?: string
  api_key_set: boolean
  api_key_masked: string
}

export interface ModelPreset {
  id: string
  label: string
  base_url: string
  model: string
  hint: string
}

export interface ModelConfigPayload {
  provider: string
  base_url: string
  api_key?: string
  model: string
  temperature?: number
  /** 备用模型链；传空串表示清空 */
  fallback_models?: string
}

export interface ModelTestResult {
  ok: boolean
  mode?: string
  latency_ms?: number
  reply?: string
  model?: string
  tokens?: number
  base_url?: string
  endpoint?: string
  /** 备用模型探测结果（配了才会返回） */
  fallbacks?: ModelFallbackProbe[]
  error?: string
}

export interface ModelFallbackProbe {
  model: string
  base_url?: string
  ok: boolean
  latency_ms?: number
  model_returned?: string
  error?: string
}

export interface ModelListResult {
  ok: boolean
  models?: string[]
  count?: number
  endpoint?: string
  error?: string
}

// ------------------------------------------------------------------ 任务目录表

/** 目录表里的一个文件。事实来自文件系统；阶段与来源来自登记表与编辑留痕。 */
export interface TreeFile {
  path: string
  name: string
  dir: string
  stem: string
  ext: string
  /** markdown / json / image / audio / video / text / other */
  kind: string
  size: number
  mtime: string
  stage_id: string
  stage_name: string
  /** model / human_edit / human_upload / human_redo / stale / backup / system / aux */
  source: string
  source_label: string
  note: string
  /** 本次成片实际用到了这个文件 */
  in_use: boolean
  /** 在 artifacts 表里有登记 */
  registered: boolean
  /** 阶段是推断出来的（文件本身没登记） */
  stage_inherited: boolean
}

export interface TreeGroup {
  id: string
  label: string
  order: number
  count: number
  bytes: number
  files: TreeFile[]
}

export interface TreeKind {
  kind: string
  label: string
  count: number
  bytes: number
}

export interface RunTree {
  run_id: string
  generated_at: string
  stats: {
    files: number
    bytes: number
    dirs: number
    stages: number
    human_touched: number
    active: number
    unassigned: number
    kinds: TreeKind[]
    edit_counts: Record<string, number>
  }
  groups_by_stage: TreeGroup[]
  groups_by_dir: TreeGroup[]
  files: TreeFile[]
}
