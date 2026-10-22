<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { api, errorMessage, openEvents } from '../api'
import ThumbGrid from '../components/ThumbGrid.vue'
import type { JournalRun } from '../types'


const inputRoot = ref('')
const outputRoot = ref('')
const move = ref(true)
const useSaucenao = ref(true)
const rescanUnknown = ref(false)
const running = ref(false)
const stopping = ref(false)
const jobId = ref('')
const error = ref('')
const logs = ref<{ text: string; error: boolean }[]>([])
const files = ref<string[]>([])
const scanned = ref(0)
const progress = ref({ done: 0, total: 0 })
const runs = ref<JournalRun[]>([])
const historyOpen = ref(false)
let source: EventSource | null = null
const pending: { text: string; error: boolean }[] = []
let decided = 0
let flushTimer: ReturnType<typeof setTimeout> | null = null

function flushLogs() {
  flushTimer = null
  if (decided) {
    progress.value.done += decided
    decided = 0
  }
  if (!pending.length) return
  const fresh = pending.splice(0, pending.length).reverse()
  logs.value = [...fresh, ...logs.value].slice(0, 400)
}

function log(text: string, failed = false) {
  pending.push({ text, error: failed })
  if (flushTimer === null) {
    flushTimer = setTimeout(flushLogs, 200)
  }
}

function fileName(path: unknown): string {
  const text = String(path ?? '')
  return text.split(/[\\/]/).pop() ?? text
}

function onEvent(kind: string, payload: Record<string, unknown>) {
  if (kind === 'snapshot') {
    const job = payload.job as Record<string, unknown> | null
    running.value = job?.status === 'running'
    if (!job) {
      stopping.value = false
      return
    }
    jobId.value = String(job.job_id ?? '')
    const stats = job.stats as Record<string, number> | undefined
    const total = Number(stats?.total ?? 0)
    if (total > 0) progress.value = { ...progress.value, total }
  } else if (kind === 'batch') {
    progress.value = { done: 0, total: Number(payload.total ?? 0) }
    log(`начато: ${payload.total} файлов`)
  } else if (kind === 'file_decided') {
    decided += 1
    const what = String(payload.fandom || payload.artist || '')
    const characters = Array.isArray(payload.characters)
      ? payload.characters.map((item) => String(item)).filter(Boolean)
      : []
    const character = characters.length
      ? `${characters.slice(0, 3).join(', ')}${characters.length > 3 ? ` +${characters.length - 3}` : ''}`
      : ''
    if (what) {
      log(character ? `${what} (${character})` : what)
    } else if (character) {
      log(character)
    } else {
      const why = [String(payload.reason || ''), String(payload.note || '')]
        .filter(Boolean)
        .join(' — ')
      log(`не найдено: ${fileName(payload.path)}${why ? ` — ${why}` : ''}`, true)
    }
  } else if (kind === 'file_failed') {
    log(`${fileName(payload.path)}: ${payload.error}`, true)
  } else if (kind === 'file_dropped') {
    log(`${fileName(payload.path)}: ${payload.reason}`, true)
  } else if (kind === 'deferred') {
    log(`отложено: ${fileName(payload.path)}`, true)
  } else if (kind === 'done') {
    running.value = false
    stopping.value = false
    const stats = payload.stats as Record<string, number> | undefined
    if (payload.status === 'failed') {
      log(`сортировка не удалась: ${payload.error}`, true)
    } else if (payload.status === 'cancelled') {
      log('остановлено', true)
    } else if (stats && stats.total !== undefined) {
      progress.value = { ...progress.value, total: stats.total }
      log(
        `итог: ${stats.total} файлов, перемещено ${stats.moved}, на проверку ${stats.review}, ` +
          `без определения ${stats.unknown}, ошибок ${stats.failed}, из кэша ${stats.reused}, ` +
          `мусора удалено ${stats.deleted ?? 0}, ${stats.elapsed} с`,
      )
    }
    void loadRuns()
  }
}

async function loadRuns() {
  try {
    const payload = await api.journal(20)
    runs.value = payload.runs
  } catch {
    runs.value = []
  }
}

async function undoRun(runId: string) {
  error.value = ''
  try {
    const result = await api.undoRun(runId)
    log(`возврат ${runId}: на место вернулось ${result.restored} из ${result.total}`)
    await loadRuns()
  } catch (problem) {
    error.value = errorMessage(problem)
  }
}

function shortTime(seconds: number): string {
  return new Date(seconds * 1000).toLocaleString()
}

async function load() {
  const settings = await api.settings()
  inputRoot.value = settings.input_root
  outputRoot.value = settings.output_root
  useSaucenao.value = settings.use_saucenao
  const current = await api.currentJob()
  running.value = current.job?.status === 'running'
  const total = Number(current.job?.stats?.total ?? 0)
  if (total > 0) progress.value = { ...progress.value, total }
}

async function start() {
  error.value = ''
  logs.value = []
  pending.length = 0
  decided = 0
  progress.value = { done: 0, total: 0 }
  try {
    const started = await api.startJob({
      input_root: inputRoot.value,
      output_root: outputRoot.value,
      move: move.value,
      use_saucenao: useSaucenao.value,
      rescan_unknown: rescanUnknown.value,
    })
    jobId.value = started.job_id
    running.value = true
  } catch (problem) {
    error.value = errorMessage(problem)
  }
}

async function stop() {
  error.value = ''
  stopping.value = true
  try {
    await api.cancelJob()
  } catch (problem) {
    error.value = errorMessage(problem)
    stopping.value = false
  }
}

async function scan() {
  error.value = ''
  try {
    const result = await api.scan(inputRoot.value)
    files.value = result.files
    scanned.value = result.count
  } catch (problem) {
    error.value = errorMessage(problem)
  }
}

onMounted(async () => {
  source = openEvents(onEvent)
  await load()
  await loadRuns()
})

onUnmounted(() => {
  source?.close()
  if (flushTimer !== null) clearTimeout(flushTimer)
})
</script>

<template>
  <div class="column tall">
    <section class="card">
      <div class="grid cols">
        <label>
          Папка ввода
          <input v-model="inputRoot" spellcheck="false" />
        </label>
        <label>
          Папка вывода
          <input v-model="outputRoot" spellcheck="false" />
        </label>
        <div class="row wrap actions">
          <button :disabled="running" @click="scan">Обновить</button>
          <button class="primary" :disabled="running" @click="start">Запустить</button>
          <button class="danger" :disabled="!running || stopping" @click="stop">
            {{ stopping ? 'Останавливается…' : 'Остановить' }}
          </button>
        </div>
      </div>
      <div class="row wrap toggles">
        <label class="check"><input v-model="move" type="checkbox" /> перемещать файлы</label>
        <label class="check">
          <input v-model="useSaucenao" type="checkbox" /> использовать SauceNAO
        </label>
        <label class="check">
          <input v-model="rescanUnknown" type="checkbox" /> переискать «не найдено»
        </label>
      </div>
      <p v-if="error" class="error">{{ error }}</p>
      <p v-if="scanned" class="muted small">в папке {{ scanned }} файлов</p>
    </section>

    <section v-if="runs.length" class="card">
      <div class="row">
        <h3>История сортировок</h3>
        <div class="row">
          <button @click="historyOpen = !historyOpen">
            {{ historyOpen ? 'Свернуть' : `Показать ${runs.length}` }}
          </button>
          <button @click="loadRuns">Обновить</button>
        </div>
      </div>
      <div v-if="historyOpen" class="history">
        <table>
          <thead>
            <tr>
              <th>Сортировка</th>
              <th style="width: 90px">Файлов</th>
              <th>Дата</th>
              <th style="width: 130px"></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="run in runs" :key="run.run_id">
              <td class="mono">{{ run.run_id }}</td>
              <td>{{ run.moves }}</td>
              <td class="muted">{{ shortTime(run.finished_at) }}</td>
              <td>
                <button class="danger" :disabled="running" @click="undoRun(run.run_id)">
                  Вернуть
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="logs">
      <div class="card log">
        <div class="row head">
          <h3>События</h3>
          <div class="row counters">
            <strong>{{ progress.done }}</strong>
            <span class="muted">/ {{ progress.total }}</span>
            <span v-if="jobId" class="mono muted">задача {{ jobId }}</span>
          </div>
        </div>
        <div class="bar">
          <div
            class="fill"
            :style="{
              width: progress.total ? `${(progress.done / progress.total) * 100}%` : '0%',
            }"
          />
        </div>
        <div class="lines mono">
          <div
            v-for="(line, index) in logs"
            :key="index"
            :class="{ failed: line.error }"
          >
            {{ line.text }}
          </div>
        </div>
      </div>
      <div v-if="files.length" class="card gridwrap">
        <ThumbGrid :paths="files" />
      </div>
    </section>
  </div>
</template>

<style scoped>
.actions {
  align-self: end;
}

.toggles {
  margin-top: var(--gap);
}

.history {
  overflow: auto;
  max-height: 30vh;
}

.history thead th {
  position: sticky;
  top: 0;
  background: var(--panel);
}

.logs {
  display: grid;
  grid-template-columns: minmax(320px, 1fr) 1.4fr;
  gap: var(--gap);
  flex: 1;
  min-height: 0;
}

.log,
.gridwrap {
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.log .head {
  justify-content: space-between;
}

.log .counters {
  gap: var(--gap-sm);
  font-size: 12px;
}

.log .lines {
  overflow: auto;
  flex: 1;
  min-height: 0;
  line-height: 1.5;
  overflow-wrap: anywhere;
  scrollbar-gutter: stable;
}

.log .failed {
  color: var(--bad);
}

.log .bar {
  margin-top: var(--gap-sm);
  margin-bottom: var(--gap-sm);
  height: 8px;
  background: var(--panel-2);
  border-radius: 999px;
  overflow: hidden;
}

.fill {
  height: 100%;
  background: var(--accent);
  transition: width 0.2s;
}
</style>
