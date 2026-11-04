<script setup lang="ts">
import type { TagProps } from 'naive-ui'
import { computed } from 'vue'
import { type Step, stepSummary, toolLabel } from '@/utils/steps'

const props = defineProps<{ step: Step; live: boolean }>()

const state = computed<'running' | 'success' | 'error' | 'stopped'>(() => {
  if (props.step.result) return props.step.result.status === 'error' ? 'error' : 'success'
  return props.live ? 'running' : 'stopped'
})
const TAG_TYPE: Record<typeof state.value, TagProps['type']> = {
  running: 'info',
  success: 'success',
  error: 'error',
  stopped: 'default',
}
const tag = computed(() => TAG_TYPE[state.value])
const text = computed(() => ({ running: '执行中', success: '成功', error: '失败', stopped: '未完成' })[state.value])

function pretty(v: unknown): string {
  return typeof v === 'string' ? v : JSON.stringify(v, null, 2)
}
</script>

<template>
  <n-collapse class="step">
    <n-collapse-item :name="step.id ?? step.name">
      <template #header>
        <span class="label">{{ toolLabel(step.name) }}</span>
        <span class="sum">{{ stepSummary(step) }}</span>
        <n-tag size="tiny" :type="tag" style="margin-left: 8px">
          <n-spin v-if="state === 'running'" :size="10" style="margin-right: 4px" />{{ text }}
        </n-tag>
      </template>
      <div class="cap">参数</div>
      <pre>{{ pretty(step.args) }}</pre>
      <template v-if="step.result">
        <div class="cap">结果</div>
        <pre>{{ pretty(step.result.content) }}</pre>
      </template>
    </n-collapse-item>
  </n-collapse>
</template>

<style scoped>
.step { box-sizing: border-box; max-width: 100%; margin: 6px 0; background: var(--app-bg); border-radius: 10px; padding: 0 12px; }
.step :deep(.n-collapse-item__header) { padding: 8px 0 !important; }
.step :deep(.n-collapse-item .n-collapse-item__header .n-collapse-item__header-main) { font-size: 13px; }
.step :deep(.n-collapse-item:not(:first-child)) { border-top: none; }
.label { font-weight: 500; }
.sum { margin-left: 8px; color: var(--muted); font-size: 12px; }
.cap { font-size: 12px; color: var(--muted); margin: 4px 0; }
pre { margin: 0; background: var(--app-bg); border-radius: 8px; padding: 8px 10px; white-space: pre-wrap; word-break: break-all; max-height: 320px; overflow: auto; font-size: 12px; }
</style>
