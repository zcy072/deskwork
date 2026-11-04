<script setup lang="ts">
import { NButton, useMessage } from 'naive-ui'
import { ref, watch } from 'vue'
import { addMemory, deleteMemory, listMemories } from '@/api/client'
import type { MemoryItem } from '@/types'
import { errMsg } from '@/utils/errors'

const MAX_CHARS = 300

const show = defineModel<boolean>('show', { required: true })
const message = useMessage()
const items = ref<MemoryItem[]>([])
const draft = ref('')
const loading = ref(false)
const loadError = ref<string | null>(null)

async function reload() {
  loading.value = true
  loadError.value = null
  try {
    items.value = await listMemories()
  } catch (e) {
    loadError.value = errMsg(e)
  } finally {
    loading.value = false
  }
}
async function add() {
  const text = draft.value.trim()
  if (!text) return
  try {
    await addMemory(text)
    draft.value = ''
    await reload()
  } catch (e) {
    message.error(`保存失败:${errMsg(e)}`)
  }
}
async function remove(id: number) {
  try {
    await deleteMemory(id)
    items.value = items.value.filter((m) => m.id !== id)
  } catch (e) {
    message.error(`删除失败:${errMsg(e)}`)
  }
}
function expiry(m: MemoryItem): string {
  return m.expires_at ? `${new Date(m.expires_at).toLocaleDateString()} 过期` : '永不过期'
}

// immediate:弹窗是首次打开时才挂载的,挂载时 show 已经是 true
watch(show, (v) => v && reload(), { immediate: true })
</script>

<template>
  <n-modal v-model:show="show" preset="card" title="长期记忆" style="width: min(560px, 92vw)">
    <p class="hint">同一个访问密钥下的所有会话共享这份记忆。内容只是备忘,不会被当作指令执行。</p>
    <div class="add">
      <n-input
        v-model:value="draft"
        type="textarea"
        :autosize="{ minRows: 1, maxRows: 3 }"
        :maxlength="MAX_CHARS"
        show-count
        placeholder="手动添加一条记忆,例如:回答默认使用中文"
      />
      <n-button type="primary" :disabled="!draft.trim()" @click="add">添加</n-button>
    </div>
    <n-spin :show="loading">
      <div class="list">
        <div v-for="m in items" :key="m.id" class="row">
          <div class="body">
            <div class="text">{{ m.content }}</div>
            <div class="meta">{{ expiry(m) }}</div>
          </div>
          <n-button text size="tiny" type="error" @click="remove(m.id)">删除</n-button>
        </div>
        <div v-if="loadError && !loading" class="load-error" role="alert">
          <span>读取记忆失败:{{ loadError }}</span>
          <n-button size="small" @click="reload">重试</n-button>
        </div>
        <n-empty v-else-if="!loading && items.length === 0" description="还没有记忆" />
      </div>
    </n-spin>
  </n-modal>
</template>

<style scoped>
.hint { margin: 0 0 12px; color: var(--muted); font-size: 13px; }
.load-error { display: flex; flex-direction: column; align-items: flex-start; gap: 8px; padding: 8px; word-break: break-word; }
.add { display: flex; gap: 8px; align-items: flex-start; margin-bottom: 12px; }
.list { display: flex; flex-direction: column; gap: 4px; max-height: 50vh; overflow-y: auto; min-height: 60px; }
.row { display: flex; gap: 12px; align-items: flex-start; justify-content: space-between; padding: 8px; border-radius: 6px; background: rgba(128,128,128,.1); }
.body { min-width: 0; }
.text { word-break: break-word; }
.meta { color: var(--muted); font-size: 12px; margin-top: 2px; }
</style>
