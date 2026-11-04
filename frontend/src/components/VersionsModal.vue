<script setup lang="ts">
import { ref, watch } from 'vue'
import { fetchFileBlob, fetchFileVersionBlob, listFileVersions, saveBlob, type FileVersion } from '@/api/client'
import type { FileRef } from '@/types'
import { errMsg } from '@/utils/errors'
import { fmtSize, toRelativePath } from '@/utils/files'
import { diffLines, type DiffLine } from '@/utils/lineDiff'
import { previewKind } from '@/utils/preview'

const props = defineProps<{ file: FileRef | null; sessionId: string }>()
const emit = defineEmits<{ close: [] }>()

const MAX_TEXT = 512 * 1024
const versions = ref<FileVersion[]>([])
const loading = ref(false)
const error = ref<string | null>(null)
const diff = ref<DiffLine[] | null>(null)
const diffNote = ref('')
const diffFor = ref<string | null>(null)
let seq = 0

const canDiff = (f: FileRef) => ['text', 'csv'].includes(previewKind(f.file_name))

watch(
  () => props.file,
  async (file) => {
    const mine = ++seq
    versions.value = []
    diff.value = null
    diffNote.value = ''
    diffFor.value = null
    error.value = null
    if (!file) return
    loading.value = true
    try {
      const list = await listFileVersions(props.sessionId, file.file_name)
      if (mine === seq) versions.value = list
    } catch (e) {
      if (mine === seq) error.value = errMsg(e)
    } finally {
      if (mine === seq) loading.value = false
    }
  },
  { immediate: true },
)

const fmtTime = (s: number) => new Date(s * 1000).toLocaleString()

async function download(v: FileVersion) {
  const f = props.file
  if (!f) return
  try {
    const dot = f.file_name.lastIndexOf('.')
    const stamp = new Date(v.modified_at * 1000).toISOString().slice(0, 19).replace(/[:T]/g, '-')
    const name = dot > 0 ? `${f.file_name.slice(0, dot)}-${stamp}${f.file_name.slice(dot)}` : `${f.file_name}-${stamp}`
    saveBlob(await fetchFileVersionBlob(props.sessionId, f.file_name, v.version), name)
  } catch (e) {
    error.value = errMsg(e)
  }
}

async function compare(v: FileVersion) {
  const f = props.file
  if (!f) return
  const mine = ++seq
  error.value = null
  diff.value = null
  diffNote.value = ''
  diffFor.value = v.version
  try {
    const [oldBlob, newBlob] = await Promise.all([
      fetchFileVersionBlob(props.sessionId, f.file_name, v.version),
      fetchFileBlob(props.sessionId, toRelativePath(f.container_path ?? '')),
    ])
    if (mine !== seq) return
    if (oldBlob.size > MAX_TEXT || newBlob.size > MAX_TEXT) {
      diffNote.value = '文件较大,不做对比,请下载后查看'
      return
    }
    const result = diffLines(await oldBlob.text(), await newBlob.text())
    if (mine !== seq) return
    if (!result) diffNote.value = '改动太多,不做逐行对比,请下载后查看'
    else if (result.every((l) => l.op === 'same')) diffNote.value = '内容与当前版本相同'
    else diff.value = result
  } catch (e) {
    if (mine === seq) error.value = errMsg(e)
  }
}
</script>

<template>
  <n-modal :show="!!file" preset="card" :title="file ? `历史版本:${file.file_name}` : ''" style="width: min(680px, 96vw)" @update:show="(v: boolean) => !v && emit('close')">
    <n-spin :show="loading">
      <n-alert v-if="error" type="error" :show-icon="false" style="margin-bottom: 8px">{{ error }}</n-alert>
      <n-empty v-if="!loading && !versions.length && !error" description="没有历史版本(文件被同名覆盖后才会保留旧版本)" />
      <div v-for="v in versions" :key="v.version" class="ver">
        <span>{{ fmtTime(v.modified_at) }} · {{ fmtSize(v.size) }}</span>
        <span class="btns">
          <n-button v-if="file && canDiff(file)" size="tiny" @click="compare(v)">与当前对比</n-button>
          <n-button size="tiny" @click="download(v)">下载</n-button>
        </span>
      </div>
      <div v-if="diffFor && diffNote" class="note">{{ diffNote }}</div>
      <pre v-if="diff" class="diff"><span v-for="(l, i) in diff" :key="i" :class="l.op">{{ ({ same: '  ', add: '+ ', del: '- ' })[l.op] + l.text }}
</span></pre>
    </n-spin>
  </n-modal>
</template>

<style scoped>
.ver { display: flex; justify-content: space-between; align-items: center; gap: 8px; padding: 8px 10px; margin-bottom: 6px; border: 1px solid var(--line); border-radius: 10px; font-size: 13px; }
.btns { display: flex; gap: 4px; flex-shrink: 0; }
.note { margin-top: 8px; color: var(--muted); font-size: 13px; }
.diff { margin: 10px 0 0; max-height: 360px; overflow: auto; font-size: 12px; line-height: 1.5; border: 1px solid var(--line); border-radius: 8px; padding: 8px; white-space: pre-wrap; word-break: break-all; }
.diff .add { display: block; background: rgba(46, 160, 67, 0.18); }
.diff .del { display: block; background: rgba(248, 81, 73, 0.18); }
.diff .same { display: block; opacity: 0.75; }
</style>
