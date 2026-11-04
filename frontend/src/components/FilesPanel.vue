<script setup lang="ts">
import { useDialog, useMessage } from 'naive-ui'
import { clearFileVersions, clearScratchpad, downloadFile } from '@/api/client'
import { useChatStore } from '@/stores/chat'
import { fmtSize, toRelativePath } from '@/utils/files'
import { previewKind } from '@/utils/preview'
import type { FileRef } from '@/types'
import { errMsg } from '@/utils/errors'
import { defineAsyncComponent, h, ref, watch } from 'vue'
import StorageUsage from './StorageUsage.vue'
import TodoList from './TodoList.vue'
import VersionsModal from './VersionsModal.vue'

// 预览抽屉体积不小且只在点预览时用到
const PreviewDrawer = defineAsyncComponent(() => import('./PreviewDrawer.vue'))

const chat = useChatStore()
const message = useMessage()
const dialog = useDialog()
const storage = ref<InstanceType<typeof StorageUsage> | null>(null)
const previewing = ref<FileRef | null>(null)
const versioning = ref<FileRef | null>(null)
// 首次预览才加载抽屉代码,之后保持挂载以保留关闭动画
const previewOpened = ref(false)
watch(previewing, (f) => f && (previewOpened.value = true))
// 预览的是上一个会话的文件,切换会话后路径已不属于当前会话
watch(() => chat.sessionId, () => {
  previewing.value = null
  versioning.value = null
})

/** 把修改要求连同目标文件路径发给 Agent;同名重新交付时旧版会自动进入"历史"。 */
async function revise(text: string) {
  const file = previewing.value
  if (!file) return
  const rel = toRelativePath(file.container_path ?? '')
  const ok = await chat.send(`请修改文件 ${rel}:${text}\n修改后用原文件名重新交付。`, [])
  if (ok) previewing.value = null
}

async function download(file: FileRef) {
  try {
    await downloadFile(chat.sessionId, toRelativePath(file.container_path ?? ''), file.file_name)
  } catch (e) {
    message.error(`下载失败:${errMsg(e)}`)
  }
}

/** 删除后文件不再能下载或预览;已发出的聊天卡片和历史版本不受影响。 */
function confirmRemove(file: FileRef) {
  dialog.warning({
    title: '删除文件',
    content: () => h('span', `确定删除「${file.file_name}」吗?删除后无法恢复。历史版本会保留。`),
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        const freed = await chat.removeFile(file)
        if (previewing.value === file) previewing.value = null
        message.success(`已删除,释放 ${fmtSize(freed)}`)
        storage.value?.refresh()
      } catch (e) {
        message.error(`删除失败:${errMsg(e)}`)
      }
    },
  })
}

const CLEAR_KINDS = {
  versions: {
    title: '清理历史版本',
    text: '确定清空所有文件的历史版本吗?删除后无法恢复,当前文件不受影响。',
    run: clearFileVersions,
  },
  scratchpad: {
    title: '清空暂存',
    text: '确定清空暂存目录吗?那是助手的中间文件,删除后无法恢复;上传和产出的文件不受影响。',
    run: clearScratchpad,
  },
} as const

function confirmClear(kind: keyof typeof CLEAR_KINDS) {
  const { title, text, run } = CLEAR_KINDS[kind]
  dialog.warning({
    title,
    content: () => h('span', text),
    positiveText: '清理',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        const freed = await run(chat.sessionId)
        message.success(`已清理,释放 ${fmtSize(freed)}`)
        storage.value?.refresh()
      } catch (e) {
        message.error(`清理失败:${errMsg(e)}`)
      }
    },
  })
}
</script>

<template>
  <aside class="files">
    <TodoList :todos="chat.todos" />
    <template v-if="chat.uploadedFiles.length">
      <div class="head">我上传的文件</div>
      <div v-for="f in chat.uploadedFiles" :key="f.container_path ?? f.file_name" class="file">
        <span class="name" :title="f.file_name">{{ f.file_name }}</span>
        <span class="btns">
          <n-button v-if="previewKind(f.file_name) !== 'none'" size="tiny" @click="previewing = f">预览</n-button>
          <n-button size="tiny" @click="download(f)">下载</n-button>
          <n-button size="tiny" :disabled="chat.running" @click="confirmRemove(f)">删除</n-button>
        </span>
      </div>
    </template>
    <div class="head">Agent 产出的文件</div>
    <n-empty v-if="chat.sharedFiles.length === 0" description="暂无文件" />
    <div v-for="f in chat.sharedFiles" :key="f.container_path ?? f.file_name" class="file">
      <span class="name" :title="f.file_name">{{ f.file_name }}</span>
      <span class="btns">
        <n-button v-if="previewKind(f.file_name) !== 'none'" size="tiny" @click="previewing = f">预览</n-button>
        <n-button size="tiny" @click="versioning = f">历史</n-button>
        <n-button size="tiny" @click="download(f)">下载</n-button>
        <n-button size="tiny" :disabled="chat.running" @click="confirmRemove(f)">删除</n-button>
      </span>
    </div>
    <StorageUsage ref="storage" @clear="confirmClear" />
    <VersionsModal :file="versioning" :session-id="chat.sessionId" @close="versioning = null" />
    <PreviewDrawer
      v-if="previewOpened"
      :file="previewing"
      :session-id="chat.sessionId"
      :busy="chat.running"
      @close="previewing = null"
      @revise="revise"
    />
  </aside>
</template>

<style scoped>
.files { width: 268px; padding: 18px 12px; border-left: 1px solid var(--line); background: var(--panel-bg); box-sizing: border-box; overflow-y: auto; }
.head { font-weight: 650; margin: 14px 0 12px; font-size: 14px; }
.file { display: flex; justify-content: space-between; align-items: center; gap: 8px; padding: 8px 10px; margin-bottom: 6px; background: var(--card-bg); border: 1px solid var(--line); border-radius: 10px; }
.btns { display: flex; gap: 4px; flex-shrink: 0; }
.name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
