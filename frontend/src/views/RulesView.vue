<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api, errorMessage } from '../api'
import type { FandomSuggestion, Rule, UnmatchedTag } from '../types'

const rules = ref<Rule[]>([])
const revision = ref('')
const unmatched = ref<UnmatchedTag[]>([])
const suggestions = ref<FandomSuggestion[]>([])
const mergeTarget = ref<Record<string, string>>({})
const filter = ref('')
const message = ref('')
const failure = ref('')


interface TagChoice {
  target: string
  name: string
}

const choices = ref<Record<string, TagChoice>>({})

function humanize(tag: string): string {
  const text = tag.replace(/_/g, ' ').replace(/\s+/g, ' ').trim()
  return text.charAt(0).toUpperCase() + text.slice(1)
}

function choice(tag: string): TagChoice {
  return choices.value[tag] ?? { target: '', name: humanize(tag) }
}

function pick(tag: string, patch: Partial<TagChoice>) {
  choices.value[tag] = { ...choice(tag), ...patch }
}

async function applyTag(tag: string) {
  const picked = choice(tag)
  const name = picked.target || picked.name.trim()
  if (!name) {
    failure.value = 'не указано название'
    return
  }
  const mode = picked.target ? 'alias' : 'new'
  failure.value = ''
  try {
    await api.ruleFromTag({ tag, mode, name })
    message.value =
      mode === 'new' ? `добавлен фандом «${name}»` : `«${name}» — добавлен вариант названия`
    await load()
  } catch (error) {
    failure.value = errorMessage(error)
  }
}

function split(value: string): string[] {
  return value
    .split(',')
    .map((part) => part.trim())
    .filter(Boolean)
}

function updateAliases(rule: Rule, value: string) {
  rule.aliases = split(value)
}

function addRule() {
  rules.value.unshift({ name: '', aliases: [], priority: 0 })
}

const visible = computed<Rule[]>(() => {
  const needle = filter.value.trim().toLowerCase()
  if (!needle) return rules.value
  return rules.value.filter((rule) =>
    [rule.name, ...rule.aliases].some((value) => value.toLowerCase().includes(needle)),
  )
})

const sortedNames = computed(() =>
  [...rules.value].sort((left, right) => left.name.localeCompare(right.name, 'ru')),
)

async function load() {
  try {
    const payload = await api.rules()
    rules.value = payload.rules
    revision.value = payload.revision
    unmatched.value = (await api.unmatched()).items
    suggestions.value = (await api.suggestions()).items
    for (const item of suggestions.value) {
      if (!mergeTarget.value[item.tag]) mergeTarget.value[item.tag] = item.similar_to
    }
  } catch (error) {
    failure.value = errorMessage(error)
  }
}

async function mergeSuggestion(item: FandomSuggestion) {
  failure.value = ''
  const name = mergeTarget.value[item.tag] || item.similar_to
  try {
    const result = await api.mergeSuggestion({ tag: item.tag, name })
    message.value = `«${item.fandom}» стал вариантом «${result.target}», файлов перенесено: ${result.moved}`
    await load()
  } catch (error) {
    failure.value = errorMessage(error)
  }
}

async function keepSuggestion(item: FandomSuggestion) {
  failure.value = ''
  try {
    await api.keepSuggestion(item.tag)
    message.value = `«${item.fandom}» остаётся отдельным фандомом`
    await load()
  } catch (error) {
    failure.value = errorMessage(error)
  }
}

async function save() {
  failure.value = ''
  try {
    const cleaned = rules.value.filter((rule) => rule.name.trim())
    const result = await api.saveRules(cleaned, revision.value)
    revision.value = result.revision
    message.value = `сохранено записей: ${result.count}`
    await load()
  } catch (error) {
    failure.value = errorMessage(error)
    if (failure.value.includes('другом окне')) {
      await load()
    }
  }
}

onMounted(load)
</script>

<template>
  <div class="column">
    <section class="card">
      <div class="row">
        <input v-model="filter" placeholder="найти в словаре" />
        <button @click="addRule">Добавить</button>
        <button class="primary" @click="save">Сохранить</button>
      </div>
      <p v-if="message" class="muted small">{{ message }}</p>
      <p v-if="failure" class="error">{{ failure }}</p>
    </section>

    <section v-if="suggestions.length" class="card">
      <h3>Похоже на известный фандом — {{ suggestions.length }}</h3>
      <p class="muted small">
        Такие теги уже заведены как отдельные фандомы и разложены по папкам. «Слить» — если это
        тот же фандом: папка переедет в существующий. «Оставить отдельно» — останется как есть.
      </p>
      <table>
        <thead>
          <tr>
            <th>Тег</th>
            <th style="width: 22%">Новый фандом</th>
            <th style="width: 30%">Похоже на</th>
            <th style="width: 70px">Схожесть</th>
            <th style="width: 210px"></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in suggestions" :key="item.tag">
            <td class="mono">{{ item.tag }}</td>
            <td>{{ item.fandom }}</td>
            <td>
              <select
                :value="mergeTarget[item.tag]"
                @change="mergeTarget[item.tag] = ($event.target as HTMLSelectElement).value"
              >
                <option v-for="rule in sortedNames" :key="rule.name" :value="rule.name">
                  {{ rule.name }}
                </option>
              </select>
            </td>
            <td>{{ item.score.toFixed(2) }}</td>
            <td class="row">
              <button class="primary" @click="mergeSuggestion(item)">Слить</button>
              <button @click="keepSuggestion(item)">Оставить отдельно</button>
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <section v-if="unmatched.length" class="card">
      <h3>Теги вне словаря — {{ unmatched.length }}</h3>
      <table>
        <thead>
          <tr>
            <th>Тег</th>
            <th style="width: 70px">Картинок</th>
            <th style="width: 32%">Существующий фандом</th>
            <th style="width: 26%">Новое название</th>
            <th style="width: 90px"></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in unmatched" :key="item.tag">
            <td class="mono">
              {{ item.tag }}
              <div v-if="item.example" class="muted small" :title="item.example">
                например: {{ item.example.split(/[\\/]/).pop() }}
              </div>
            </td>
            <td>{{ item.hits }}</td>
            <td>
              <select
                :value="choice(item.tag).target"
                @change="pick(item.tag, { target: ($event.target as HTMLSelectElement).value })"
              >
                <option value="">— не выбрано —</option>
                <option v-for="rule in sortedNames" :key="rule.name" :value="rule.name">
                  {{ rule.name }}
                </option>
              </select>
            </td>
            <td>
              <input
                :value="choice(item.tag).name"
                :disabled="!!choice(item.tag).target"
                spellcheck="false"
                @input="pick(item.tag, { name: ($event.target as HTMLInputElement).value })"
              />
            </td>
            <td><button class="primary" @click="applyTag(item.tag)">Применить</button></td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="card tablewrap">
      <table>
        <thead>
          <tr>
            <th style="width: 32%">Основное название</th>
            <th style="width: 56%">Варианты названия</th>
            <th style="width: 8%">Приоритет</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(rule, index) in visible" :key="`${rule.name}-${index}`">
            <td><input v-model="rule.name" /></td>
            <td>
              <input
                :value="rule.aliases.join(', ')"
                @input="updateAliases(rule, ($event.target as HTMLInputElement).value)"
              />
            </td>
            <td><input v-model.number="rule.priority" type="number" /></td>
            <td><button class="danger" @click="rules.splice(rules.indexOf(rule), 1)">×</button></td>
          </tr>
        </tbody>
      </table>
    </section>
  </div>
</template>

<style scoped>
.tablewrap {
  overflow: auto;
  max-height: 50vh;
}
</style>
