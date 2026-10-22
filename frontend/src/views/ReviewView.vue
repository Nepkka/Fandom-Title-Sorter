<script setup lang="ts">
import { computed, onActivated, onMounted, ref, watch } from 'vue'
import { api, errorMessage, thumbUrl } from '../api'
import type { FileDetails, FileRow, TargetPreview } from '../types'

const rows = ref<FileRow[]>([])
const details = ref<FileDetails | null>(null)
const selected = ref<number | null>(null)
const failure = ref('')
const message = ref('')
const filter = ref('')

const kindLabel: Record<string, string> = {
  fandom: 'фандом',
  original: 'оригинал',
  unknown: 'не распознано',
}

function kindText(kind: string): string {
  return kindLabel[kind] ?? kind
}

const form = ref({
  fandom: '',
  character: '',
  artist: '',
  multi_character: false,
})

const preview = ref<TargetPreview | null>(null)
const previewFailure = ref('')
let previewTimer: ReturnType<typeof setTimeout> | null = null

function schedulePreview() {
  if (previewTimer) clearTimeout(previewTimer)
  if (selected.value === null) return
  previewTimer = setTimeout(refreshPreview, 250)
}

function currentKind(): 'fandom' | 'original' | 'unknown' {
  if (form.value.fandom.trim()) return 'fandom'
  if (form.value.artist.trim()) return 'original'
  return 'unknown'
}

async function refreshPreview() {
  if (selected.value === null) return
  previewFailure.value = ''
  try {
    preview.value = await api.previewTarget(selected.value, {
      kind: currentKind(),
      fandom: form.value.fandom || null,
      character: form.value.character || null,
      artist: form.value.artist || null,
      multi_character: form.value.multi_character,
    })
  } catch (error) {
    preview.value = null
    previewFailure.value = errorMessage(error)
  }
}

watch(form, schedulePreview, { deep: true })

async function load() {
  try {
    rows.value = (await api.files(300)).items
  } catch (error) {
    failure.value = errorMessage(error)
  }
}

async function open(imageId: number) {
  selected.value = imageId
  message.value = ''
  failure.value = ''
  details.value = await api.fileDetails(imageId)
  const decision = details.value.decision
  form.value = {
    fandom: decision?.fandom ?? '',
    character: decision?.character ?? '',
    artist: decision?.artist ?? '',
    multi_character: decision?.multi_character ?? false,
  }
  schedulePreview()
}

function pickAlternative(fandom: string) {
  form.value.fandom = fandom
}

async function apply(move: boolean) {
  if (selected.value === null) return
  failure.value = ''
  const kind = currentKind()
  try {
    const result = await api.setDecision(selected.value, {
      kind,
      fandom: form.value.fandom || null,
      character: form.value.character || null,
      artist: form.value.artist || null,
      multi_character: form.value.multi_character,
      move,
    })
    await load()
    await open(selected.value)
    message.value = result.target ? `перенесено: ${result.target}` : 'решение сохранено'
  } catch (error) {
    failure.value = errorMessage(error)
  }
}

async function undo() {
  if (selected.value === null) return
  failure.value = ''
  try {
    const result = await api.undoFile(selected.value)
    await load()
    await open(selected.value)
    message.value = `возвращено: ${result.restored}`
  } catch (error) {
    failure.value = errorMessage(error)
  }
}

const visible = computed<FileRow[]>(() => {
  const needle = filter.value.trim().toLowerCase()
  if (!needle) return rows.value
  return rows.value.filter((row) =>
    [row.fandom, row.character, row.artist, row.path, row.reason]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(needle)),
  )
})

onMounted(load)
onActivated(load)
</script>

<template>
  <div class="split">
    <section class="card list">
      <div class="row">
        <input v-model="filter" placeholder="фильтр по фандому, персонажу, автору, пути" />
        <button @click="load">Обновить</button>
      </div>
      <p v-if="failure" class="error">{{ failure }}</p>
      <table v-if="visible.length">
        <thead>
          <tr>
            <th style="width: 64px"></th>
            <th>Файл</th>
            <th style="width: 90px">Похожесть</th>
            <th>Комментарий</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in visible"
            :key="row.image_id"
            :class="{ active: selected === row.image_id }"
            @click="open(row.image_id)"
          >
            <td><img :src="thumbUrl(row.image_id, 96)" loading="lazy" /></td>
            <td>
              <span class="badge" :class="row.kind">{{ kindText(row.kind) }}</span>
              <span v-if="row.action === 'review'" class="badge review">на проверку</span>
              <div>{{ row.fandom ?? '—' }}</div>
              <div class="muted">{{ row.character ?? row.artist ?? '' }}</div>
            </td>
            <td>{{ row.confidence }}</td>
            <td class="muted">{{ row.reason }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="!rows.length" class="muted">Пока ничего не разобрано</p>
    </section>
    <section class="card details">
      <template v-if="details">
        <img class="preview" :src="thumbUrl(details.image.id, 512)" />

        <h3>Решение по файлу</h3>
        <label>
          Фандом
          <input v-model="form.fandom" spellcheck="false" />
        </label>
        <label>
          Персонаж
          <input
            v-model="form.character"
            spellcheck="false"
            :disabled="form.multi_character"
            placeholder="несколько персонажей — папка MultiChar"
          />
        </label>
        <label>
          Автор
          <input v-model="form.artist" spellcheck="false" />
        </label>
        <label class="check">
          <input v-model="form.multi_character" type="checkbox" />
          несколько персонажей
        </label>

        <p v-if="preview" class="preview-line">
          переместится сюда: <span class="mono">{{ preview.relative }}</span>
          <span v-if="!preview.folder_exists" class="muted"> (папка будет создана)</span>
          <span v-if="preview.name_taken" class="muted"> (имя занято — добавится номер)</span>
        </p>
        <p v-else-if="previewFailure" class="muted">{{ previewFailure }}</p>

        <div v-if="details.decision?.alternatives.length" class="alternatives">
          <span class="muted">предложения источников:</span>
          <button
            v-for="alt in details.decision.alternatives"
            :key="alt.fandom"
            @click="pickAlternative(alt.fandom)"
          >
            {{ alt.fandom }}
          </button>
        </div>

        <div class="row wrap actions">
          <button @click="apply(false)">Сохранить</button>
          <button class="primary" @click="apply(true)">Сохранить и переместить</button>
          <button class="danger" @click="undo">Вернуть на место</button>
        </div>
        <p v-if="message" class="muted small">{{ message }}</p>

        <h3>О файле</h3>
        <table>
          <tbody>
            <tr><th>Путь</th><td class="mono">{{ details.image.path }}</td></tr>
            <tr><th>Разрешение</th><td>{{ details.image.width }}×{{ details.image.height }}</td></tr>
            <tr v-if="details.decision">
              <th>Источник</th>
              <td>{{ details.decision.manual ? 'Вручную' : 'Автоматически' }}</td>
            </tr>
            <tr v-if="details.decision">
              <th>Уверенность</th><td>{{ details.decision.confidence }}</td>
            </tr>
          </tbody>
        </table>

        <h4>Что нашли источники</h4>
        <table v-if="details.searches.length">
          <thead>
            <tr><th>Источник</th><th>Похожесть</th><th>Пост</th></tr>
          </thead>
          <tbody>
            <tr v-for="(signal, index) in details.searches" :key="index">
              <td>{{ signal.source }}</td>
              <td>{{ signal.similarity }}</td>
              <td class="mono muted">{{ signal.post_url }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="muted small">источники не записаны — этот файл разбирала старая версия</p>
      </template>
      <p v-else class="muted">Выбери файл слева.</p>
    </section>
  </div>
</template>

<style scoped>
label:not(.check) {
  margin-bottom: var(--gap-sm);
}

.list,
.details {
  overflow: auto;
}

tr.active {
  background: var(--panel-2);
}

tbody tr {
  cursor: pointer;
}

img {
  max-width: 100%;
  border-radius: 6px;
}

.preview {
  max-height: 220px;
  object-fit: contain;
  background: var(--panel-2);
  margin-bottom: var(--gap);
}

.preview-line {
  margin: var(--gap-sm) 0 var(--gap);
  font-size: 12px;
  color: var(--muted);
  word-break: break-all;
}

.alternatives {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  align-items: center;
  margin: var(--gap) 0;
  font-size: 12px;
}

.actions {
  margin: var(--gap) 0;
}
</style>
