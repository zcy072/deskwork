<script setup lang="ts">
import { NButton, useDialog, useMessage } from 'naive-ui'
import { computed, defineAsyncComponent, h, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { downloadSessionArchive, getCapabilities, importSessionArchive, getMessages, saveBlob, searchConversations } from '@/api/client'
import type { ConversationSummary, SearchHit } from '@/types'
import { disableNotifications, enableNotifications, notificationsEnabled, notificationsSupported } from '@/utils/attention'
import { errMsg } from '@/utils/errors'
import { isImeEnter } from '@/utils/keys'
import { conversationToMarkdown, exportFileName } from '@/utils/exportMarkdown'
import { groupByRecency } from '@/utils/recency'
import { runBadge } from '@/utils/runBadge'
import { useChatStore } from '@/stores/chat'

defineProps<{ themeLabel: string }>()
defineEmits<{ toggleTheme: []; editKey: [] }>()

const MemoryModal = defineAsyncComponent(() => import('@/components/MemoryModal.vue'))

const chat = useChatStore()
const dialog = useDialog()
const message = useMessage()
const editing = ref<string | null>(null)
const notifyOn = ref(notificationsEnabled())
async function toggleNotify() {
  if (notifyOn.value) {
    disableNotifications()
    notifyOn.value = false
    return
  }
  notifyOn.value = await enableNotifications()
  if (!notifyOn.value) message.warning('浏览器没有授权通知,请在站点设置中允许')
}
const draft = ref('')
const memoryEnabled = ref(false)
const showMemory = ref(false)
// 首次打开时才加载弹窗代码,之后保持挂载以保留关闭动画
const memoryOpened = ref(false)
watch(showMemory, (v) => v && (memoryOpened.value = true))

// 后端没开记忆功能时不显示入口;读取失败也按没开处理
onMounted(async () => {
  memoryEnabled.value = await getCapabilities().then((c) => c.memory, () => false)
})

// 按更新时间分组;每次列表变化重新计算("今天"的边界以当前时间为准)
const badge = (c: ConversationSummary) => runBadge(c, chat.isGenerating(c.id), c.id === chat.sessionId)
const groups = computed(() => groupByRecency(chat.conversations))

// 搜索:输入停顿 300ms 后查询;seq 防止慢的旧请求覆盖新结果
const query = ref('')
const hits = ref<SearchHit[] | null>(null)
const searchError = ref<string | null>(null)
let searchSeq = 0
let searchTimer: ReturnType<typeof setTimeout> | null = null
watch(query, (v) => {
  if (searchTimer) clearTimeout(searchTimer)
  const q = v.trim()
  const mine = ++searchSeq
  if (!q) {
    hits.value = null
    searchError.value = null
    return
  }
  searchTimer = setTimeout(async () => {
    try {
      const r = await searchConversations(q)
      if (mine === searchSeq) {
        hits.value = r
        searchError.value = null
      }
    } catch (e) {
      if (mine === searchSeq) searchError.value = errMsg(e)
    }
  }, 300)
})
onBeforeUnmount(() => searchTimer && clearTimeout(searchTimer))
function openHit(id: string) {
  void chat.openConversation(id)
}

function startRename(id: string, title: string) {
  editing.value = id
  draft.value = title
}
async function commitRename() {
  const id = editing.value
  editing.value = null
  if (id && draft.value.trim()) await chat.rename(id, draft.value.trim())
}
async function exportConversation(id: string, title: string) {
  try {
    const md = conversationToMarkdown(title, await getMessages(id))
    saveBlob(new Blob([md], { type: 'text/markdown;charset=utf-8' }), exportFileName(title))
  } catch (e) {
    message.error(`导出失败:${errMsg(e)}`)
  }
}
const importInput = ref<HTMLInputElement | null>(null)
const importing = ref(false)
async function onImportPicked(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  importing.value = true
  try {
    const r = await importSessionArchive(file)
    await chat.refreshConversations()
    await chat.openConversation(r.id)
    const notes = [...r.warnings]
    if (r.missing.length) notes.push(`${r.missing.length} 个文件在备份时已不存在`)
    const text = `已导入为新会话「${r.title || '未命名'}」${r.resumable ? ',可继续对话' : ''}`
    if (notes.length) message.warning(`${text}。${notes.join(';')}`, { duration: 8000 })
    else message.success(text)
  } catch (err) {
    message.error(`导入失败:${errMsg(err)}`)
  } finally {
    importing.value = false
  }
}
async function backupConversation(id: string) {
  try {
    await downloadSessionArchive(id)
  } catch (e) {
    message.error(`备份失败:${errMsg(e)}`)
  }
}
function confirmDelete(id: string, title: string) {
  dialog.warning({
    title: '删除会话',
    content: () => h('span', `确定删除「${title}」吗?对话记录与沙箱文件都会被清除,无法恢复。`),
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: () => chat.removeConversation(id),
  })
}
</script>

<template>
  <aside class="sidebar">
    <div class="brand">Deskwork</div>
    <n-button type="primary" block @click="chat.newConversation()">新对话</n-button>
    <n-input v-model:value="query" size="small" clearable placeholder="搜索对话" class="search" aria-label="搜索对话" />
    <div v-if="query.trim()" class="list">
      <div v-if="searchError" class="list-error" role="alert">搜索失败:{{ searchError }}</div>
      <n-empty v-else-if="hits && hits.length === 0" description="没有匹配的对话" style="margin-top: 24px" />
      <div v-for="hit in hits ?? []" :key="hit.id" class="item hit" :class="{ active: hit.id === chat.sessionId }" @click="openHit(hit.id)">
        <button type="button" class="title" :title="hit.title" @click.stop="openHit(hit.id)">
          {{ hit.title }}
          <span v-if="hit.snippet" class="snippet">{{ hit.snippet }}</span>
        </button>
      </div>
    </div>
    <div v-else class="list">
      <template v-for="g in groups" :key="g.label">
      <div class="group">{{ g.label }}</div>
      <div
        v-for="c in g.items"
        :key="c.id"
        class="item"
        :class="{ active: c.id === chat.sessionId }"
        @click="chat.openConversation(c.id)"
      >
        <n-input
          v-if="editing === c.id"
          v-model:value="draft"
          size="small"
          autofocus
          @click.stop
          @keydown.enter="(e: KeyboardEvent) => !isImeEnter(e) && commitRename()"
          @blur="commitRename"
        />
        <template v-else>
          <button
            type="button"
            class="title"
            :title="c.title"
            :aria-current="c.id === chat.sessionId ? 'true' : undefined"
            @click.stop="chat.openConversation(c.id)"
          >
            {{ c.title }}
          </button>
          <span
            v-if="badge(c)"
            :class="['dot', badge(c)!.kind]"
            :title="badge(c)!.label"
            role="status"
            :aria-label="badge(c)!.label"
          />
          <span class="actions" @click.stop>
            <n-button text size="tiny" @click="startRename(c.id, c.title)">重命名</n-button>
            <n-button text size="tiny" type="error" @click="confirmDelete(c.id, c.title)">删除</n-button>
            <n-button text size="tiny" @click="exportConversation(c.id, c.title)">导出</n-button>
            <n-button text size="tiny" title="打包消息与文件(zip)" @click="backupConversation(c.id)">备份</n-button>
          </span>
        </template>
      </div>
      </template>
      <n-button
        v-if="chat.conversationsHasMore"
        size="tiny"
        quaternary
        block
        :loading="chat.loadingMoreConversations"
        @click="chat.loadMoreConversations()"
      >
        加载更多
      </n-button>
      <div v-if="chat.conversationsError" class="list-error" role="alert">
        <div>会话列表加载失败:{{ chat.conversationsError }}</div>
        <n-button size="tiny" @click="chat.refreshConversations()">重试</n-button>
      </div>
      <n-empty v-else-if="chat.conversations.length === 0" description="还没有对话" style="margin-top: 24px" />
    </div>
    <div class="foot">
      <n-button v-if="memoryEnabled" quaternary size="small" block @click="showMemory = true">长期记忆</n-button>
      <n-button quaternary size="small" block :loading="importing" @click="importInput?.click()">导入备份</n-button>
      <input ref="importInput" type="file" accept=".zip,application/zip" hidden aria-label="选择备份包" @change="onImportPicked" />
      <n-button quaternary size="small" block @click="$emit('editKey')">访问密钥</n-button>
      <n-button v-if="notificationsSupported()" quaternary size="small" block @click="toggleNotify">后台通知:{{ notifyOn ? '开' : '关' }}</n-button>
      <n-button quaternary size="small" block @click="$emit('toggleTheme')">主题:{{ themeLabel }}</n-button>
    </div>
    <MemoryModal v-if="memoryEnabled && memoryOpened" v-model:show="showMemory" />
  </aside>
</template>

<style scoped>
.sidebar { width: 268px; display: flex; flex-direction: column; gap: 12px; padding: 18px 12px 12px; border-right: 1px solid var(--line); box-sizing: border-box; background: var(--panel-bg); }
.brand { display: flex; align-items: center; gap: 10px; font-weight: 650; font-size: 17px; letter-spacing: .2px; padding: 0 6px 2px; }
.brand::before { content: ''; width: 22px; height: 22px; border-radius: 7px; background: linear-gradient(135deg, var(--accent), var(--accent-2)); flex-shrink: 0; }
.list { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 2px; margin: 0 -4px; padding: 0 4px; }
.item { display: flex; align-items: center; justify-content: space-between; padding: 9px 10px; border-radius: 9px; cursor: pointer; font-size: 14px; transition: background .12s; }
.item:hover { background: var(--soft); }
.item.active { background: var(--soft-strong); color: var(--accent); font-weight: 500; }
.title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; min-width: 0; text-align: left; background: none; border: 0; padding: 0; margin: 0; font: inherit; color: inherit; cursor: pointer; }
.group { padding: 10px 10px 4px; font-size: 12px; color: var(--muted); }
.search { margin: 8px 0 4px; }
.hit .title { white-space: normal; }
.snippet { display: block; font-size: 12px; opacity: .65; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dot { width: 7px; height: 7px; border-radius: 50%; background: var(--accent); flex-shrink: 0; margin: 0 6px; }
.dot.running { animation: gen-pulse 1.2s ease-in-out infinite; }
.dot.waiting { background: #f0a020; }
.dot.error { background: #d03050; }
.dot.cancelled { background: var(--muted); }
@keyframes gen-pulse { 50% { opacity: .3; } }
.title:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }
.actions { display: flex; gap: 8px; margin-left: 8px; opacity: 0; transition: opacity .12s; }
.item:hover .actions, .item:focus-within .actions { opacity: 1; }
@media (hover: none) { .actions { opacity: 1; } }
.list-error { margin: 16px 6px; font-size: 13px; color: var(--muted); display: flex; flex-direction: column; gap: 8px; align-items: flex-start; word-break: break-word; }
.foot { display: flex; flex-direction: column; gap: 2px; border-top: 1px solid var(--line); padding-top: 8px; }
.foot :deep(.n-button) { justify-content: flex-start; color: var(--muted); }
</style>
