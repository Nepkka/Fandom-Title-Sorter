
import type {
  CheckState,
  DecisionPayload,
  FandomSuggestion,
  FileDetails,
  FileRow,
  JournalRun,
  JobState,
  ProxyStatus,
  Rule,
  RuleSet,
  ScanResult,
  Settings,
  TargetPreview,
  UnmatchedTag,
} from './types'

const TOKEN_KEY = 'fts_token'

function readToken(): string {
  const fromUrl = new URLSearchParams(window.location.search).get('token')
  if (fromUrl) {
    localStorage.setItem(TOKEN_KEY, fromUrl)
    const clean = new URL(window.location.href)
    clean.searchParams.delete('token')
    window.history.replaceState({}, '', clean.toString())
    return fromUrl
  }
  return localStorage.getItem(TOKEN_KEY) ?? ''
}

export const token = readToken()

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  headers.set('X-FTS-Token', token)
  if (init.body) headers.set('Content-Type', 'application/json')
  const response = await fetch(path, { ...init, headers })
  if (!response.ok) {
    let detail = `HTTP ${response.status}`
    try {
      const payload = await response.json()
      if (payload?.detail) detail = String(payload.detail)
    } catch {
      detail = `HTTP ${response.status}`
    }
    throw new Error(detail)
  }
  return (await response.json()) as T
}

export const api = {
  settings: () => request<Settings>('/api/settings'),
  saveSettings: (payload: Partial<Settings>) =>
    request<Settings>('/api/settings', { method: 'PUT', body: JSON.stringify(payload) }),

  scan: (root: string) =>
    request<ScanResult>('/api/scan', { method: 'POST', body: JSON.stringify({ root }) }),

  files: (limit: number) => request<{ items: FileRow[] }>(`/api/files?limit=${limit}`),
  fileDetails: (id: number) => request<FileDetails>(`/api/files/${id}`),
  setDecision: (id: number, payload: DecisionPayload) =>
    request<{ target: string | null }>(`/api/files/${id}/decision`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  undoFile: (id: number) =>
    request<{ restored: string }>(`/api/files/${id}/undo`, { method: 'POST' }),
  previewTarget: (id: number, payload: DecisionPayload) =>
    request<TargetPreview>(`/api/files/${id}/target`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  startJob: (payload: Record<string, unknown>) =>
    request<{ job_id: string }>('/api/jobs', { method: 'POST', body: JSON.stringify(payload) }),
  currentJob: () => request<{ job: JobState | null; history: JobState[] }>('/api/jobs/current'),
  cancelJob: () => request<{ cancelled: boolean }>('/api/jobs/current', { method: 'DELETE' }),

  journal: (limit = 20) =>
    request<{ runs: JournalRun[]; queued: number }>(`/api/journal?limit=${limit}`),
  undoRun: (runId: string) =>
    request<{ restored: number; total: number }>(
      `/api/journal/${encodeURIComponent(runId)}/undo`,
      { method: 'POST' },
    ),

  rules: () => request<RuleSet>('/api/rules'),
  saveRules: (rules: Rule[], revision: string) =>
    request<{ count: number; revision: string }>('/api/rules', {
      method: 'PUT',
      headers: revision ? { 'If-Match': revision } : undefined,
      body: JSON.stringify(rules),
    }),
  unmatched: () => request<{ count: number; items: UnmatchedTag[] }>('/api/unmatched'),
  ruleFromTag: (payload: { tag: string; mode: 'alias' | 'new'; name: string }) =>
    request<{ count: number }>('/api/rules/from-tag', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  suggestions: () => request<{ count: number; items: FandomSuggestion[] }>('/api/suggestions'),
  mergeSuggestion: (payload: { tag: string; name: string }) =>
    request<{ target: string; moved: number }>('/api/suggestions/merge', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  keepSuggestion: (tag: string) =>
    request<{ tag: string; kept: boolean }>('/api/suggestions/keep', {
      method: 'POST',
      body: JSON.stringify({ tag }),
    }),

  proxies: () => request<ProxyStatus>('/api/proxies'),
  refreshProxies: () =>
    request<{ total: number; report: Record<string, number> }>('/api/proxies/refresh', {
      method: 'POST',
    }),
  checkProxies: (limit: number) =>
    request<CheckState>('/api/proxies/check', {
      method: 'POST',
      body: JSON.stringify({ limit }),
    }),
  checkState: () => request<CheckState>('/api/proxies/check'),
  stopCheck: () => request<{ stopped: boolean }>('/api/proxies/check', { method: 'DELETE' }),
}

export function thumbUrl(imageId: number, size: number): string {
  return `/api/thumbs/${imageId}?size=${size}&token=${encodeURIComponent(token)}`
}

export function thumbByPathUrl(path: string, size: number): string {
  return `/api/thumbs/by-path?path=${encodeURIComponent(path)}&size=${size}&token=${encodeURIComponent(token)}`
}

export function openEvents(onEvent: (kind: string, payload: Record<string, unknown>) => void) {
  const source = new EventSource(`/api/events?token=${encodeURIComponent(token)}`)
  const kinds = [
    'snapshot',
    'batch',
    'file_started',
    'file_decided',
    'file_failed',
    'file_dropped',
    'deferred',
    'done',
  ]
  for (const kind of kinds) {
    source.addEventListener(kind, (event) => {
      const message = event as MessageEvent<string>
      let payload: Record<string, unknown> = {}
      try {
        payload = JSON.parse(message.data)?.payload ?? {}
      } catch {
        payload = {}
      }
      onEvent(kind, payload)
    })
  }
  return source
}
