<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, errorMessage } from '../api'
import type { Settings } from '../types'

const settings = ref<Settings | null>(null)
const inputRoot = ref('')
const outputRoot = ref('')
const sepCharacters = ref(true)
const concurrency = ref(32)
const limit = ref(0)
const message = ref('')
const failure = ref('')
const saucenaoKeys = ref('')
const keysMessage = ref('')

async function load() {
  try {
    const payload = await api.settings()
    settings.value = payload
    inputRoot.value = payload.input_root
    outputRoot.value = payload.output_root
    sepCharacters.value = payload.sep_characters
    concurrency.value = payload.concurrency
    limit.value = payload.limit
    saucenaoKeys.value = payload.saucenao_keys
  } catch (error) {
    failure.value = errorMessage(error)
  }
}

function keyCount(): number {
  return saucenaoKeys.value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean).length
}

async function saveKeys() {
  keysMessage.value = ''
  failure.value = ''
  try {
    settings.value = await api.saveSettings({ saucenao_keys: saucenaoKeys.value })
    saucenaoKeys.value = settings.value.saucenao_keys
    keysMessage.value = `сохранено ключей: ${keyCount()}`
  } catch (error) {
    failure.value = errorMessage(error)
  }
}

async function save() {
  failure.value = ''
  try {
    settings.value = await api.saveSettings({
      input_root: inputRoot.value,
      output_root: outputRoot.value,
      sep_characters: sepCharacters.value,
      concurrency: concurrency.value,
      limit: limit.value,
    })
    message.value = 'сохранено'
  } catch (error) {
    failure.value = errorMessage(error)
  }
}

onMounted(load)
</script>

<template>
  <div class="column narrow">
    <section class="card">
      <h3>Папки</h3>
      <div class="grid cols">
        <label>
          Папка ввода
          <input v-model="inputRoot" spellcheck="false" />
        </label>
        <label>
          Папка вывода
          <input v-model="outputRoot" spellcheck="false" />
        </label>
      </div>

      <h3>Раскладка</h3>
      <div class="row wrap">
        <label class="check">
          <input v-model="sepCharacters" type="checkbox" />
          раскладывать по персонажам
        </label>
      </div>
      <h3>Скорость и лимиты</h3>
      <div class="grid cols">
        <label>
          Одновременно обрабатывать файлов
          <input v-model.number="concurrency" type="number" min="1" max="256" />
        </label>
        <label>
          Лимит файлов за сортировку
          <input v-model.number="limit" type="number" min="0" placeholder="без лимита" />
        </label>
        <div v-if="settings" class="muted limits">
          <div>запросов к одному хосту за раз: {{ settings.limits.per_host_concurrency }}</div>
          <div>
            SauceNAO: не больше {{ settings.limits.saucenao_per_window }} запросов за
            {{ settings.limits.saucenao_window_seconds }} с с одного адреса,
            {{ settings.limits.saucenao_daily_budget_per_ip }} в сутки
          </div>
        </div>
      </div>

      <div class="row save">
        <button class="primary" @click="save">Сохранить</button>
        <span v-if="message" class="muted small">{{ message }}</span>
        <span v-if="failure" class="error small">{{ failure }}</span>
      </div>
    </section>

    <details class="card keys">
      <summary>Ключи SauceNAO{{ keyCount() ? ` — ${keyCount()}` : '' }}</summary>
      <p class="muted small">По одному ключу в строке</p>
      <textarea
        v-model="saucenaoKeys"
        rows="8"
        spellcheck="false"
        placeholder="вставь ключи, по одному в строке"
      />
      <div class="row save">
        <button class="primary" @click="saveKeys">Сохранить ключи</button>
        <span v-if="keysMessage" class="muted small">{{ keysMessage }}</span>
      </div>
    </details>
  </div>
</template>

<style scoped>
.save {
  margin-top: var(--gap);
}

.limits {
  align-self: end;
  font-size: 12px;
  line-height: 1.6;
}

.keys summary {
  cursor: pointer;
  color: var(--muted);
  font-size: 13px;
}

.keys textarea {
  width: 100%;
  min-height: 120px;
  margin-top: var(--gap-sm);
  padding: 8px 10px;
  background: var(--panel-2);
  color: var(--text);
  border: 1px solid var(--line);
  border-radius: 6px;
  font-family: Consolas, 'Cascadia Mono', monospace;
  font-size: 12px;
  resize: vertical;
}

.keys textarea:focus {
  outline: 1px solid var(--accent);
}
</style>
