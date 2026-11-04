<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { fetchFileBlob, saveBlob } from '@/api/client'
import type { SourceRef } from '@/types'
import { errMsg } from '@/utils/errors'
import { toRelativePath } from '@/utils/files'
import { locationLabel, sourceLabel } from '@/utils/sources'

const props = defineProps<{ source: SourceRef | null; sessionId: string }>()
const emit = defineEmits<{ close: [] }>()
const loading = ref(false)
const error = ref<string | null>(null)
const url = ref<string | null>(null)
const blob = ref<Blob | null>(null)
const lines = ref<{ number: number; text: string; selected: boolean }[]>([])
let seq = 0
const title = computed(() => props.source ? sourceLabel(props.source) : '原文引用')
const pdfUrl = computed(() => url.value && props.source?.location.kind === 'page' ? `${url.value}#page=${props.source.location.page}` : null)
function reset() {
  if (url.value) URL.revokeObjectURL(url.value)
  url.value = null
  blob.value = null
  lines.value = []
  error.value = null
}
watch(() => [props.source, props.sessionId] as const, async ([source, sid]) => {
  const mine = ++seq
  reset()
  loading.value = false
  if (!source) return
  loading.value = true
  try {
    // 服务端对实际返回的字节校验摘要,文件变化时返回 409,不展示错位的原文。
    const raw = await fetchFileBlob(sid, toRelativePath(source.container_path), source.fingerprint)
    if (mine !== seq) return
    blob.value = raw
    if (source.location.kind === 'slide' || source.location.kind === 'paragraphs') {
      // 幻灯片和 Word 段落只展示检索时的摘录;不渲染原文件,但仍已校验文件未变化,可下载原文件
    } else if (source.location.kind === 'page') {
      url.value = URL.createObjectURL(new Blob([raw], { type: 'application/pdf' }))
    } else {
      const text = await raw.text()
      if (mine !== seq) return
      const { start, end } = source.location
      lines.value = text.split('\n').slice(Math.max(0, start - 4), end + 3).map((text, i) => {
        const number = Math.max(1, start - 3) + i
        return { number, text, selected: number >= start && number <= end }
      })
    }
  } catch (e) {
    if (mine === seq) error.value = errMsg(e)
  } finally {
    if (mine === seq) loading.value = false
  }
}, { immediate: true })
onBeforeUnmount(() => { ++seq; reset() })
function download() {
  if (blob.value && props.source) saveBlob(blob.value, props.source.file_name)
}
</script>

<template>
  <n-modal :show="!!source" preset="card" :title="title" style="width: min(800px, 96vw)" @update:show="(v: boolean) => !v && emit('close')">
    <template v-if="source">
      <div class="caption">检索时的摘录</div>
      <pre class="excerpt">{{ source.excerpt }}</pre>
      <n-spin v-if="loading" size="small" />
      <n-alert v-else-if="error" type="warning" role="alert">{{ error }}</n-alert>
      <iframe v-else-if="pdfUrl" :src="pdfUrl" class="pdf" :title="title" />
      <div v-else-if="source.location.kind === 'slide'" class="caption">
        来自第 {{ source.location.slide }} 张幻灯片;文件内容与检索时一致,可下载原文件查看完整版式。
      </div>
      <div v-else-if="source.location.kind === 'paragraphs'" class="caption">
        来自正文{{ locationLabel(source.location) }}(按非空段落计数,表格算一段);文件内容与检索时一致,可下载原文件查看完整版式。
      </div>
      <pre v-else class="context"><span v-for="line in lines" :key="line.number" :class="{ selected: line.selected }"><span class="number">{{ line.number }}</span>{{ line.text }}
</span></pre>
      <n-button :disabled="!blob || !!error" size="small" @click="download">下载原文件</n-button>
    </template>
  </n-modal>
</template>

<style scoped>
.caption { font-size: 12px; color: var(--muted); }
.excerpt, .context { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 13px; max-height: 240px; overflow: auto; padding: 12px; background: var(--soft); border-radius: 8px; }
.context { max-height: 50vh; }
.context > span { display: block; }
.selected { background: var(--soft-strong); }
.number { display: inline-block; min-width: 3em; margin-right: 12px; color: var(--muted); user-select: none; }
.pdf { width: 100%; height: 60vh; border: 0; margin-bottom: 12px; }
</style>
