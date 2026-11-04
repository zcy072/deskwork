<script setup lang="ts">
import { computed, ref } from 'vue'
import { ApiError, downloadFile, fetchDeliveredBlob, saveBlob } from '@/api/client'
import { useChatStore } from '@/stores/chat'
import type { FileRef } from '@/types'
import { errMsg } from '@/utils/errors'
import { toRelativePath } from '@/utils/files'

const props = defineProps<{ files: FileRef[] }>()
const chat = useChatStore()
const error = ref('')

/** 同一个文件在会话里被交付过多次时,最后一次交付才是"最新版本",此前的才需要区分。 */
const latest = computed(() => {
  const last = new Map<string, string | undefined>()
  for (const m of chat.messages)
    for (const p of m.parts)
      if (p.type === 'files') for (const f of p.files) last.set(f.container_path ?? f.file_name, f.sha256)
  return last
})

const isOutdated = (f: FileRef) => !!f.sha256 && latest.value.get(f.container_path ?? f.file_name) !== f.sha256

async function downloadDelivered(f: FileRef) {
  if (!f.sha256) return
  error.value = ''
  try {
    saveBlob(await fetchDeliveredBlob(chat.sessionId, f.file_name, f.sha256, f.size), f.file_name)
  } catch (e) {
    error.value = e instanceof ApiError && e.status === 410 ? '这个版本已不在保留范围内,请下载最新版本' : `下载失败:${errMsg(e)}`
  }
}

async function downloadLatest(f: FileRef) {
  error.value = ''
  try {
    await downloadFile(chat.sessionId, toRelativePath(f.container_path ?? ''), f.file_name)
  } catch (e) {
    error.value = `下载失败:${errMsg(e)}`
  }
}
</script>

<template>
  <div class="delivered">
    <div v-for="f in files" :key="`${f.container_path ?? f.file_name}:${f.sha256 ?? ''}`" class="item">
      <span class="name" :title="f.file_name">{{ f.file_name }}</span>
      <span v-if="isOutdated(f)" class="tag">已有更新版本</span>
      <span class="btns">
        <n-button v-if="f.sha256" size="tiny" @click="downloadDelivered(f)">{{ isOutdated(f) ? '下载当时版本' : '下载' }}</n-button>
        <n-button v-if="isOutdated(f) || !f.sha256" size="tiny" @click="downloadLatest(f)">下载最新版本</n-button>
      </span>
    </div>
    <div v-if="error" class="error" role="alert">{{ error }}</div>
  </div>
</template>

<style scoped>
.delivered { display: flex; flex-direction: column; gap: 6px; margin: 8px 0; }
.item { display: flex; align-items: center; gap: 8px; padding: 6px 10px; border: 1px solid var(--line); border-radius: 10px; font-size: 13px; }
.name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
.error { color: #d03050; font-size: 12px; }
.tag { color: var(--muted); font-size: 12px; flex-shrink: 0; }
.btns { display: flex; gap: 4px; margin-left: auto; flex-shrink: 0; }
</style>
