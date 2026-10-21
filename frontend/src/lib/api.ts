import type {
  CanvasData,
  CanvasFrame,
  CanvasModels,
  CanvasPanel,
  FileContent,
  FrameRole,
  MediaConfigState,
  MediaTestResult,
  ModelConfigPayload,
  ModelListResult,
  ModelPreset,
  ModelStatus,
  ModelTestResult,
  RunDetail,
  RunEvent,
  RunSummary,
  RunTree,
  SkillDetail,
  SkillSummary,
  StageFiles,
} from './types'

const BASE = '/api'

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`${res.status} ${res.statusText} ${text.slice(0, 200)}`)
  }
  return res.json() as Promise<T>
}

export const api = {
  meta: () => req<{ llm_mode: string; model: string; channels: Record<string, any>; quota: Record<string, number> }>('/meta'),
  skills: () => req<{ skills: SkillSummary[] }>('/skills'),
  skill: (id: string) => req<SkillDetail>(`/skills/${id}`),
  runs: () => req<{ runs: RunSummary[] }>('/runs'),
  run: (id: string) => req<RunDetail>(`/runs/${id}`),
  createRun: (body: { skill_id: string; title?: string; brief?: Record<string, any>; channel?: string; preset_id?: string | null; autostart?: boolean }) =>
    req<RunSummary>('/runs', { method: 'POST', body: JSON.stringify(body) }),
  resolveGate: (runId: string, gateId: string, body: { status: string; feedback?: string; patches?: Record<string, any> }) =>
    req<{ ok: boolean; revived?: boolean }>(`/runs/${runId}/gates/${gateId}/resolve`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  cancelRun: (runId: string) => req<{ ok: boolean }>(`/runs/${runId}/cancel`, { method: 'POST' }),
  /** 草稿 → 精修：原地续跑，复用已完成阶段的产物 */
  upgradeRun: (runId: string) =>
    req<{ ok: boolean; channel: string; pending_stages: string[] }>(`/runs/${runId}/upgrade`, {
      method: 'POST',
    }),
  /** 接着跑：中断/失败后补跑未完成阶段，不换通道、不重做已完成阶段 */
  resumeRun: (runId: string) =>
    req<{ ok: boolean; channel: string; reuse: string[]; pending_stages: string[] }>(
      `/runs/${runId}/resume`,
      { method: 'POST' },
    ),
  fileUrl: (runId: string, rel: string) => `${BASE}/runs/${runId}/files/${rel}`,
  downloadUrl: (runId: string) => `${BASE}/runs/${runId}/download`,

  // ---- 任务目录表 ----
  runTree: (runId: string) => req<RunTree>(`/runs/${runId}/tree`),
  exportTree: (runId: string, groupBy: 'stage' | 'dir' = 'stage') =>
    req<{ ok: boolean; path: string; bytes: number; markdown: string }>(
      `/runs/${runId}/tree/export?group_by=${groupBy}&save=true`,
      { method: 'POST' },
    ),

  // ---- 模型配置 ----
  modelConfig: () =>
    req<{ status: ModelStatus; presets: ModelPreset[]; env_file: string; env_file_exists: boolean }>(
      '/settings/model',
    ),
  saveModelConfig: (body: ModelConfigPayload) =>
    req<{ ok: boolean; status: ModelStatus; saved_to: string; message: string }>('/settings/model', {
      method: 'PUT',
      body: JSON.stringify(body),
    }),
  testModelConfig: (body: ModelConfigPayload) =>
    req<ModelTestResult>('/settings/model/test', { method: 'POST', body: JSON.stringify(body) }),
  listModels: (body: { base_url: string; api_key?: string }) =>
    req<ModelListResult>('/settings/model/models', { method: 'POST', body: JSON.stringify(body) }),

  // ---- 画布编辑（提示词 / 角色 / 单格重生 / 上传 / 阶段级模型）----
  canvas: (runId: string) => req<CanvasData>(`/runs/${runId}/canvas`),
  canvasModels: (runId: string) => req<CanvasModels>(`/runs/${runId}/canvas/models`),
  savePanels: (runId: string, panels: Partial<CanvasPanel>[]) =>
    req<{ ok: boolean; edited: string[]; unknown: string[]; note?: string }>(`/runs/${runId}/panels`, {
      method: 'PUT',
      body: JSON.stringify({ panels }),
    }),
  regeneratePanel: (
    runId: string,
    index: number,
    body: { stage_id?: string; prompt?: string; ratio?: string; image_model?: string; image_service?: string },
  ) =>
    req<{ ok: boolean; panel: CanvasPanel; model: string; service: string }>(
      `/runs/${runId}/panels/${index}/regenerate`,
      { method: 'POST', body: JSON.stringify(body) },
    ),
  uploadPanelImage: (runId: string, index: number, body: { data_url: string; note?: string }) =>
    req<{ ok: boolean; path: string; size: number; index: string }>(`/runs/${runId}/panels/${index}/image`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  saveCharacters: (runId: string, text: string) =>
    req<{ ok: boolean; chars: number; backed_up_origin: boolean }>(`/runs/${runId}/characters`, {
      method: 'PUT',
      body: JSON.stringify({ text }),
    }),
  charactersOrigin: (runId: string) => req<{ text: string }>(`/runs/${runId}/characters/origin`),
  setStageOverride: (runId: string, stageId: string, body: Record<string, string>) =>
    req<{ stage_id: string; override: Record<string, string>; changed: string[] }>(
      `/runs/${runId}/stages/${stageId}/overrides`,
      { method: 'PUT', body: JSON.stringify(body) },
    ),
  uploadStageRef: (runId: string, stageId: string, body: { data_url: string; note?: string }) =>
    req<{ ok: boolean; path: string; size: number; stage_id: string }>(`/runs/${runId}/stages/${stageId}/refs`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  // ---- 大模型修改建议（只读；采纳与否由人决定）----
  suggestPanel: (runId: string, index: number, body: { note?: string }) =>
    req<{
      ok: boolean
      index: number
      diagnosis: string
      patch: Record<string, string>
      why: Record<string, string>
      model: string
    }>(`/runs/${runId}/panels/${index}/suggest`, { method: 'POST', body: JSON.stringify(body) }),
  suggestStage: (runId: string, stageId: string, body: { note?: string }) =>
    req<{
      ok: boolean
      stage_id: string
      overall: string
      suggestions: { target: string; issue: string; suggestion: string; priority: string }[]
      model: string
      override_model?: string | null
    }>(`/runs/${runId}/stages/${stageId}/suggest`, { method: 'POST', body: JSON.stringify(body) }),

  // ---- 多帧（首 / 中 / 尾）----
  uploadFrame: (runId: string, index: number, role: FrameRole, body: { data_url: string; note?: string }) =>
    req<{ ok: boolean; path: string; size: number; index: string; role: FrameRole }>(
      `/runs/${runId}/panels/${index}/frames/${role}`,
      { method: 'POST', body: JSON.stringify(body) },
    ),
  regenerateFrame: (
    runId: string,
    index: number,
    role: FrameRole,
    body: { stage_id?: string; prompt?: string; ratio?: string; image_model?: string; image_service?: string },
  ) =>
    req<{ ok: boolean; index: string; role: FrameRole; frame: CanvasFrame }>(
      `/runs/${runId}/panels/${index}/frames/${role}/regenerate`,
      { method: 'POST', body: JSON.stringify(body) },
    ),
  deleteFrame: (runId: string, index: number, role: FrameRole) =>
    req<{ ok: boolean }>(`/runs/${runId}/panels/${index}/frames/${role}`, { method: 'DELETE' }),

  // ---- 视频片段 ----
  generatePanelVideo: (
    runId: string,
    index: number,
    body: { prompt?: string; duration?: number; mode?: string; model?: string },
  ) =>
    req<{ ok: boolean; status: string; duration: number; mode: string; out: string; message: string }>(
      `/runs/${runId}/panels/${index}/video`,
      { method: 'POST', body: JSON.stringify(body) },
    ),
  uploadReferenceVideo: (
    runId: string,
    index: number,
    body: { data_url?: string; url?: string },
  ) =>
    req<{ ok: boolean; path: string; size: number; reference_video: string }>(
      `/runs/${runId}/panels/${index}/reference-video`,
      {
        method: 'POST',
        body: JSON.stringify(body),
      },
    ),
  deleteReferenceVideo: (runId: string, index: number) =>
    req<{ ok: boolean }>(`/runs/${runId}/panels/${index}/reference-video`, { method: 'DELETE' }),
  deletePanelVideo: (runId: string, index: number) =>
    req<{ ok: boolean }>(`/runs/${runId}/panels/${index}/video`, { method: 'DELETE' }),

  // ---- 阶段产物编辑（任何步骤）----
  stageFiles: (runId: string, stageId: string) => req<StageFiles>(`/runs/${runId}/stages/${stageId}/files`),
  fileContent: (runId: string, path: string) =>
    req<FileContent>(`/runs/${runId}/artifact/content?path=${encodeURIComponent(path)}`),
  saveFileContent: (runId: string, body: { path: string; content: string; stage_id?: string }) =>
    req<{ ok: boolean; path: string; chars: number; changed: boolean; backed_up_origin?: boolean; note?: string }>(
      `/runs/${runId}/artifact/content`,
      { method: 'PUT', body: JSON.stringify(body) },
    ),
  uploadStageFile: (
    runId: string,
    body: { path: string; data_url: string; stage_id?: string; note?: string },
  ) =>
    req<{ ok: boolean; path: string; size: number; bytes: number }>(`/runs/${runId}/artifact/upload`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  restoreStageFile: (runId: string, body: { path: string; stage_id?: string }) =>
    req<{ ok: boolean; path: string; restored_from: string }>(`/runs/${runId}/artifact/restore`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  // ---- 媒体配置（图片 / 语音 / 视频，文本见 modelConfig）----
  mediaConfig: () => req<MediaConfigState>('/settings/media'),
  saveMediaConfig: (kind: 'image' | 'tts' | 'video', values: Record<string, any>) =>
    req<{ ok: boolean; kind: string; values: Record<string, any>; ready: { ready: boolean; hint: string }; message: string }>(
      `/settings/media/${kind}`,
      { method: 'PUT', body: JSON.stringify({ values }) },
    ),
  testMediaConfig: (kind: 'image' | 'tts' | 'video', body: { confirm_cost: boolean; values: Record<string, any> }) =>
    req<MediaTestResult>(`/settings/media/${kind}/test`, { method: 'POST', body: JSON.stringify(body) }),
}

/**
 * 订阅 Run 的事件流。
 * 使用浏览器原生 EventSource —— 它在断线后会自动重连，
 * 并按规范带上 Last-Event-ID，后端据此补齐丢失的事件（长任务必须扛住刷新）。
 */
export function openRunStream(runId: string, onEvent: (e: RunEvent) => void, onState?: (s: 'open' | 'connecting' | 'closed') => void) {
  onState?.('connecting')
  const es = new EventSource(`${BASE}/runs/${runId}/events`)

  es.onopen = () => onState?.('open')
  es.onerror = () => onState?.('connecting') // EventSource 会自动重连
  es.onmessage = (ev) => {
    try {
      const data = JSON.parse(ev.data) as RunEvent
      if (data && data.type) onEvent(data)
    } catch {
      /* 忽略解析失败的心跳等 */
    }
  }
  return () => {
    es.close()
    onState?.('closed')
  }
}
