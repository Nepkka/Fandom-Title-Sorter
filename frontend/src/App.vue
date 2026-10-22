<script setup lang="ts">
import { computed, ref } from 'vue'
import { token } from './api'
import MonitorView from './views/MonitorView.vue'
import ReviewView from './views/ReviewView.vue'
import RulesView from './views/RulesView.vue'
import ProxiesView from './views/ProxiesView.vue'
import SettingsView from './views/SettingsView.vue'

const tabs = [
  { id: 'monitor', label: 'Сортировка', view: MonitorView },
  { id: 'review', label: 'Файлы', view: ReviewView },
  { id: 'rules', label: 'Словарь', view: RulesView },
  { id: 'proxies', label: 'Прокси', view: ProxiesView },
  { id: 'settings', label: 'Настройки', view: SettingsView },
] as const

const active = ref<string>('monitor')

const current = computed(() => tabs.find((tab) => tab.id === active.value)?.view ?? MonitorView)

const hasToken = token.length > 0
</script>

<template>
  <div class="shell">
    <header>
      <div class="brand">
        <strong>Fandom Title Sorter</strong>
      </div>
    </header>

    <nav>
      <button
        v-for="tab in tabs"
        :key="tab.id"
        :class="{ primary: active === tab.id }"
        @click="active = tab.id"
      >
        {{ tab.label }}
      </button>
    </nav>

    <main>
      <p v-if="!hasToken" class="card">
        Токен не найден — ссылка с токеном напечатана в окне лаунчера.
      </p>
      <KeepAlive v-else>
        <component :is="current" />
      </KeepAlive>
    </main>
  </div>
</template>

<style scoped>
.shell {
  display: flex;
  flex-direction: column;
  height: 100vh;
}

header {
  padding: var(--gap) 16px;
  border-bottom: 1px solid var(--line);
  background: var(--panel);
}

.brand {
  display: flex;
  gap: var(--gap-sm);
  align-items: baseline;
}

nav {
  display: flex;
  gap: 6px;
  padding: var(--gap) 16px;
  border-bottom: 1px solid var(--line);
}

main {
  flex: 1;
  overflow: auto;
  padding: 16px;
}
</style>
