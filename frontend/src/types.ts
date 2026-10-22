
export interface Limits {
  per_host_concurrency: number
  saucenao_per_window: number
  saucenao_window_seconds: number
  saucenao_daily_budget_per_ip: number
}

export interface Settings {
  input_root: string
  output_root: string
  sep_characters: boolean
  concurrency: number
  limit: number
  saucenao_keys: string
  limits: Limits
}

export interface TargetPreview {
  relative: string
  folder_exists: boolean
  name_taken: boolean
}

export interface JournalRun {
  run_id: string
  moves: number
  finished_at: number
}

export interface ScanResult {
  count: number
  files: string[]
}

export interface Decision {
  kind: 'fandom' | 'original' | 'unknown'
  fandom: string | null
  character: string | null
  artist: string | null
  multi_character: boolean
  confidence: number
  reason: string
  manual: boolean
  alternatives: { fandom: string; evidence: number; best_similarity: number }[]
}

export interface DecisionPayload {
  kind: string
  fandom?: string | null
  character?: string | null
  artist?: string | null
  multi_character?: boolean
  move?: boolean
}

export interface FileRow {
  image_id: number
  path: string
  kind: string
  action: string
  fandom: string | null
  character: string | null
  artist: string | null
  confidence: number
  reason: string
}

export interface SearchRow {
  source: string
  post_url: string | null
  similarity: number
}

export interface ImageRow {
  id: number
  path: string
  ext: string
  size: number
  width: number
  height: number
  mtime: number
  md5: string | null
  phash: number | null
  seen_at: number
}

export interface FileDetails {
  image: ImageRow
  decision: Decision | null
  searches: SearchRow[]
}

export interface JobState {
  job_id: string
  status: 'running' | 'done' | 'cancelled' | 'failed'
  stats: Record<string, number>
  error: string | null
  started_at: number
  finished_at: number
  events_seen: number
  input_root: string
  output_root: string
  move: boolean
}

export interface Rule {
  name: string
  aliases: string[]
  priority: number
}

export interface RuleSet {
  revision: string
  rules: Rule[]
}

export interface UnmatchedTag {
  tag: string
  hits: number
  example: string | null
}

export interface FandomSuggestion {
  tag: string
  fandom: string
  similar_to: string
  score: number
}

export interface ProxyStatus {
  total: number
  hosts: Record<string, number>
  targets: Record<string, string>
  skipped: string[]
}

export interface CheckState {
  running: boolean
  total: number
  checked: number
  dropped: number
  error: string | null
}
