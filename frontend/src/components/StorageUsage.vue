<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { getStorage, type StorageInfo } from '@/api/client'
import { useChatStore } from '@/stores/chat'
import { fmtSize } from '@/utils/files'

const chat = useChatStore()
const info = ref<StorageInfo | null>(null)
let seq = 0

const LABELS: [keyof StorageInfo['sizes'], string][] = [
  ['uploads', '上传'],
  ['output', '产出'],
  ['scratchpad', '暂存'],
  ['versions', '历史版本'],
  ['snapshots', '回退快照'],
]

const rows = computed(() => (info.value ? LABELS.map(([k, label]) => ({ k, label, size: info.value!.sizes[k] })) : []))
const ratio = computed(() => (info.value && info.value.quota_limit ? info.value.quota_used / info.value.quota_limit : 0))

async function refresh() {
  const mine = ++seq
  const sid = chat.sessionId
  if (!sid || chat.messages.length === 0) {
    info.value = null
    return
  }
  try {
    const next = await getStorage(sid)
    if (mine === seq) info.value = next
  } catch {
    if (mine === seq) info.value = null // 查不到只是不显示占用,不影响文件操作
  }
}

// 会话切换、每轮生成结束、增删文件后刷新
watch(() => [chat.sessionId, chat.running, chat.sharedFiles.length, chat.uploadedFiles.length], refresh, { immediate: true })
const emit = defineEmits<{ clear: [kind: 'versions' | 'scratchpad'] }>()
defineExpose({ refresh })
</script>

<template>
  <div v-if="info" class="storage" aria-label="存储占用">
    <div class="head">存储占用</div>
    <n-progress
      v-if="info.quota_limit"
      type="line"
      :percentage="Math.min(100, Math.round(ratio * 100))"
      :status="ratio >= 0.9 ? 'warning' : 'default'"
      :show-indicator="false"
    />
    <div class="total">配额内 {{ fmtSize(info.quota_used) }}<template v-if="info.quota_limit"> / {{ fmtSize(info.quota_limit) }}</template></div>
    <div v-for="r in rows" :key="r.k" class="row">
      <span>{{ r.label }}</span>
      <span>
        <button v-if="(r.k === 'versions' || r.k === 'scratchpad') && r.size > 0" class="clear" type="button" @click="emit('clear', r.k)">清理</button>
        {{ fmtSize(r.size) }}
      </span>
    </div>
    <div class="note">历史版本与回退快照不计入配额。</div>
  </div>
</template>

<style scoped>
.storage { margin-top: 18px; padding-top: 4px; border-top: 1px solid var(--line); font-size: 13px; }
.head { font-weight: 650; margin: 12px 0 8px; font-size: 14px; }
.total { margin: 6px 0; }
.row { display: flex; justify-content: space-between; color: var(--muted); line-height: 1.7; }
.clear { border: 0; background: none; padding: 0 6px 0 0; color: var(--accent); cursor: pointer; font-size: 12px; }
.note { margin-top: 6px; font-size: 12px; color: var(--muted); }
</style>
