<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { downloadFile, fetchFileBlob, fetchPreviewBlob, saveBlob } from '@/api/client'
import type { FileRef } from '@/types'
import { toRelativePath } from '@/utils/files'
import { mimeOf, parseCsv, previewKind } from '@/utils/preview'
import { errMsg } from '@/utils/errors'
import { isImeEnter } from '@/utils/keys'

const props = defineProps<{ file: FileRef | null; sessionId: string; busy?: boolean }>()
const emit = defineEmits<{ close: []; revise: [text: string] }>()

const request = ref('')
const canSubmit = computed(() => !props.busy && !!request.value.trim())
function onRevisionEnter(e: KeyboardEvent) {
  if (isImeEnter(e)) return
  e.preventDefault()
  submitRevision()
}
function submitRevision() {
  if (!canSubmit.value) return
  emit('revise', request.value.trim())
  request.value = ''
}

/** 窄屏上不超过视口宽度 */
const drawerWidth = 'min(640px, 100vw)'
const MAX_TEXT = 1024 * 1024
const loading = ref(false)
const error = ref<string | null>(null)
const url = ref<string | null>(null)
const text = ref('')
const rows = ref<string[][]>([])
const truncated = ref(false)
const blob = ref<Blob | null>(null)
let seq = 0

const kind = computed(() => (props.file ? previewKind(props.file.file_name) : 'none'))

function reset() {
  if (url.value) URL.revokeObjectURL(url.value)
  url.value = null
  text.value = ''
  rows.value = []
  truncated.value = false
  error.value = null
  blob.value = null
}

watch(
  () => [props.file, props.sessionId] as const,
  async ([file, sessionId]) => {
    const mine = ++seq // 先作废上一次还在进行的加载,否则关闭后它返回会泄漏 blob URL
    reset()
    loading.value = false
    if (!file) return
    const k = previewKind(file.file_name)
    if (k === 'none') return
    loading.value = true
    try {
      const rel = toRelativePath(file.container_path ?? '')
      const raw = k === 'office' ? await fetchPreviewBlob(sessionId, rel) : await fetchFileBlob(sessionId, rel)
      if (mine !== seq) return // 已切换到别的文件
      // 以扩展名为准重设类型,避免服务端猜错
      const b = k === 'office' ? new Blob([raw], { type: 'application/pdf' }) : k === 'image' || k === 'pdf' ? new Blob([raw], { type: mimeOf(file.file_name) }) : raw
      blob.value = b
      if (k === 'image' || k === 'pdf' || k === 'office') url.value = URL.createObjectURL(b)
      else {
        truncated.value = b.size > MAX_TEXT
        const t = await b.slice(0, MAX_TEXT).text()
        if (mine !== seq) return
        if (k === 'csv') rows.value = parseCsv(t)
        else text.value = t
      }
    } catch (e) {
      if (mine === seq) error.value = errMsg(e)
    } finally {
      if (mine === seq) loading.value = false
    }
  },
  // 抽屉是首次点预览时才异步挂载的,挂载时 file 已有值,不 immediate 就永远不会加载
  { immediate: true },
)
onBeforeUnmount(() => {
  ++seq
  reset()
})

async function save() {
  if (!props.file || !blob.value) return
  // office 预览的 blob 是转换后的 PDF,下载要给原文件
  if (kind.value === 'office') {
    try {
      await downloadFile(props.sessionId, toRelativePath(props.file.container_path ?? ''), props.file.file_name)
    } catch (e) {
      error.value = errMsg(e)
    }
    return
  }
  saveBlob(blob.value, props.file.file_name)
}
</script>

<template>
  <n-drawer :show="!!file" :width="drawerWidth" placement="right" @update:show="(v: boolean) => !v && emit('close')">
    <n-drawer-content :title="file?.file_name" closable>
      <n-spin v-if="loading" style="width: 100%; margin-top: 40px">
        <template v-if="kind === 'office'" #description>正在转换为 PDF,首次约需十几秒…</template>
      </n-spin>
      <n-alert v-else-if="error" type="error">{{ error }}</n-alert>
      <n-empty v-else-if="kind === 'none'" description="暂不支持预览该类型,请下载后查看" />
      <img v-else-if="kind === 'image' && url" :src="url" class="img" :alt="file?.file_name" />
      <iframe v-else-if="(kind === 'pdf' || kind === 'office') && url" :src="url" class="pdf" title="PDF 预览" />
      <div v-else-if="kind === 'csv'" class="tbl">
        <table>
          <tr v-for="(r, i) in rows" :key="i" :class="{ h: i === 0 }">
            <td v-for="(c, j) in r" :key="j">{{ c }}</td>
          </tr>
        </table>
        <n-text v-if="rows.length >= 200" depth="3">仅显示前 200 行</n-text>
      </div>
      <pre v-else-if="kind === 'text'" class="txt">{{ text }}</pre>
      <n-text v-if="truncated" depth="3">文件超过 1 MB,仅显示开头部分</n-text>
      <template #footer>
        <div class="foot">
          <n-input
            v-model:value="request"
            type="textarea"
            :autosize="{ minRows: 1, maxRows: 4 }"
            :disabled="busy"
            :placeholder="busy ? '正在生成,完成后可提出修改' : '对这个文件提出修改要求…'"
            @keydown.enter.exact="onRevisionEnter"
          />
          <n-button type="primary" :disabled="!canSubmit" @click="submitRevision">提交修改</n-button>
          <n-button :disabled="!blob" @click="save">下载</n-button>
        </div>
      </template>
    </n-drawer-content>
  </n-drawer>
</template>

<style scoped>
.img { max-width: 100%; }
.foot { display: flex; gap: 8px; align-items: flex-end; width: 100%; }
.foot :deep(.n-input) { flex: 1; }
.pdf { width: 100%; height: 75vh; border: 0; }
.txt { white-space: pre-wrap; word-break: break-all; font-size: 12px; margin: 0; }
.tbl { overflow: auto; }
table { border-collapse: collapse; font-size: 12px; }
td { border: 1px solid rgba(128,128,128,.35); padding: 3px 8px; white-space: nowrap; }
tr.h td { font-weight: 600; background: rgba(128,128,128,.12); }
</style>
