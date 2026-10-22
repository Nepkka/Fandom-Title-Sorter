<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { api, errorMessage } from '../api'
import type { CheckState, ProxyStatus } from '../types'

const status = ref<ProxyStatus | null>(null)
const check = ref<CheckState | null>(null)
const target = ref(200)
const busy = ref('')
const message = ref('')
const failure = ref('')
let timer: ReturnType<typeof setTimeout> | null = null


async function load() {
  try {
    status.value = await api.proxies()
  } catch (error) {
    failure.value = errorMessage(error)
  }
}

async function refresh() {
  busy.value = 'сбор списков'
  message.value = ''
  try {
    const result = await api.refreshProxies()
    message.value = `прокси в базе: ${result.total}, отбраковано нерабочих: ${result.report._dropped_dead ?? 0}`
    await load()
  } catch (error) {
    failure.value = errorMessage(error)
  } finally {
    busy.value = ''
  }
}

async function startCheck() {
  busy.value = 'проверка'
  message.value = ''
  try {
    check.value = await api.checkProxies(target.value)
    poll()
  } catch (error) {
    failure.value = errorMessage(error)
  } finally {
    busy.value = ''
  }
}

async function stopCheck() {
  try {
    await api.stopCheck()
    message.value = 'проверка остановлена'
  } catch (error) {
    failure.value = errorMessage(error)
  }
}

function poll() {
  if (timer) clearTimeout(timer)
  timer = setTimeout(async () => {
    try {
      check.value = await api.checkState()
    } catch (error) {
      failure.value = errorMessage(error)
      return
    }
    if (check.value?.running) {
      poll()
    } else {
      if (check.value?.error) {
        failure.value = check.value.error
      } else if (check.value) {
        message.value = `проверено ${check.value.checked}, отбраковано: ${check.value.dropped}`
      }
      await load()
    }
  }, 1000)
}

onMounted(async () => {
  await load()
  try {
    check.value = await api.checkState()
    if (check.value?.running) poll()
  } catch (error) {
    failure.value = errorMessage(error)
  }
})

onBeforeUnmount(() => {
  if (timer) clearTimeout(timer)
})
</script>

<template>
  <div class="column">
    <section class="card">
      <div class="row">
        <label class="target">
          Прокси для проверки
          <input v-model.number="target" type="number" min="1" />
        </label>
        <button class="row-end" :disabled="!!busy" @click="refresh">Собрать списки</button>
        <button
          v-if="!check?.running"
          class="row-end"
          :disabled="!!busy"
          @click="startCheck"
        >
          Проверить
        </button>
        <button v-else class="row-end danger" @click="stopCheck">Прервать</button>
      </div>
      <p v-if="busy" class="muted small">{{ busy }}…</p>
      <p v-if="check?.running" class="muted small">
        проверено: {{ check.checked }} из {{ check.total }}
      </p>
      <p v-if="message" class="muted small">{{ message }}</p>
      <p v-if="failure" class="error">{{ failure }}</p>
      <p class="muted small">всего прокси: {{ status?.total ?? 0 }}</p>
    </section>

    <section class="card">
      <table>
        <thead>
          <tr>
            <th>Хост</th>
            <th style="width: 120px">Рабочих</th>
            <th>Проверочный адрес</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(url, host) in status?.targets ?? {}" :key="host">
            <td>{{ host }}</td>
            <td>
              <span
                v-if="status?.skipped.includes(host)"
                class="badge unknown"
                title="прокси здесь не помогут: борд берётся браузером или заблокирован на уровне сети"
              >
                —
              </span>
              <span
                v-else
                class="badge"
                :class="(status?.hosts[host] ?? 0) > 0 ? 'fandom' : 'unknown'"
              >
                {{ status?.hosts[host] ?? 0 }}
              </span>
            </td>
            <td class="mono muted url">{{ url }}</td>
          </tr>
        </tbody>
      </table>
    </section>
  </div>
</template>

<style scoped>
.target {
  max-width: 220px;
}

.row-end {
  align-self: end;
}

.url {
  overflow-wrap: anywhere;
}
</style>
