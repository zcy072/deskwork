<script setup lang="ts">
import { computed } from 'vue'
import type { Widget } from '@/types'
import { buildWidgetDoc } from '@/utils/widget'

const props = defineProps<{ widget: Widget }>()
const doc = computed(() => buildWidgetDoc(props.widget.html_content))
// 高度由模型给出,可能缺失、为 0 或大得离谱;限制在合理范围内
const height = computed(() => {
  const h = Number(props.widget.height)
  return Number.isFinite(h) && h > 0 ? Math.min(Math.max(h, 120), 1200) : 500
})
</script>

<template>
  <div class="widget">
    <div class="wtitle">{{ widget.title || '可视化' }}</div>
    <!-- sandbox 不含 allow-same-origin:widget 脚本无法访问本站存储与 API Key -->
    <iframe
      :title="widget.title || '可视化'"
      sandbox="allow-scripts"
      referrerpolicy="no-referrer"
      :srcdoc="doc"
      :style="{ height: height + 'px' }"
    />
  </div>
</template>

<style scoped>
.widget { border: 1px solid rgba(128,128,128,.3); border-radius: 8px; overflow: hidden; margin: 8px 0; }
.wtitle { padding: 6px 12px; font-size: 13px; background: rgba(128,128,128,.12); }
iframe { width: 100%; border: 0; background: #fff; display: block; }
</style>
