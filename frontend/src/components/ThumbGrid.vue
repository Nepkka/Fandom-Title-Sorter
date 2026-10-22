<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { thumbByPathUrl } from '../api'

const DEFAULT_HEIGHT = 600

const props = withDefaults(
  defineProps<{ paths: string[]; size?: number; columns?: number }>(),
  { size: 192, columns: 8 },
)

const container = ref<HTMLElement | null>(null)
const scrollTop = ref(0)
const height = ref(DEFAULT_HEIGHT)

const cellHeight = computed(() => props.size + 30)
const rows = computed(() => Math.ceil(props.paths.length / props.columns))
const totalHeight = computed(() => rows.value * cellHeight.value)

const visible = computed(() => {
  const first = Math.max(0, Math.floor(scrollTop.value / cellHeight.value) - 1)
  const last = first + Math.ceil(height.value / cellHeight.value) + 2
  const slice = props.paths.slice(first * props.columns, last * props.columns)
  return slice.map((path, index) => ({
    path,
    name: path.split(/[\\/]/).pop() ?? path,
    top: Math.floor((first * props.columns + index) / props.columns) * cellHeight.value,
    left: ((first * props.columns + index) % props.columns) * 100 / props.columns,
  }))
})

function measure() {
  scrollTop.value = container.value?.scrollTop ?? 0
  height.value = container.value?.clientHeight ?? DEFAULT_HEIGHT
}

onMounted(() => {
  measure()
  window.addEventListener('resize', measure)
})
onUnmounted(() => window.removeEventListener('resize', measure))
</script>

<template>
  <div ref="container" class="viewport" @scroll="measure">
    <div class="spacer" :style="{ height: `${totalHeight}px` }">
      <figure
        v-for="item in visible"
        :key="item.path"
        :style="{
          top: `${item.top}px`,
          left: `${item.left}%`,
          width: `${100 / props.columns}%`,
          height: `${cellHeight}px`,
        }"
      >
        <img :src="thumbByPathUrl(item.path, props.size)" loading="lazy" :alt="item.name" />
        <figcaption :title="item.path">{{ item.name }}</figcaption>
      </figure>
    </div>
  </div>
</template>

<style scoped>
.viewport {
  overflow: auto;
  height: 100%;
}

.spacer {
  position: relative;
}

figure {
  position: absolute;
  margin: 0;
  padding: 6px 8px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
}

img {
  max-width: 100%;
  max-height: calc(100% - 26px);
  object-fit: contain;
  border-radius: 6px;
  background: var(--panel-2);
}

figcaption {
  font-size: 11px;
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}
</style>
