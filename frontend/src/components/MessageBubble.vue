<script setup lang="ts">
import { computed, defineAsyncComponent, ref, watch } from 'vue'
import { useChatStore } from '@/stores/chat'
import type { ChatMessage, SourceRef } from '@/types'
import { sourceLabel } from '@/utils/sources'
import { copyText } from '@/utils/clipboard'
import { renderMarkdown } from '@/utils/markdown'
import { groupParts } from '@/utils/steps'
import AskCard from './AskCard.vue'
import DeliveredFiles from './DeliveredFiles.vue'
import StepCard from './StepCard.vue'
import WidgetFrame from './WidgetFrame.vue'

const props = defineProps<{ message: ChatMessage; live?: boolean }>()
const chat = useChatStore()
const isUser = computed(() => props.message.role === 'user')
const SourcePreview = defineAsyncComponent(() => import('./SourcePreview.vue'))
const previewing = ref<SourceRef | null>(null)
const previewOpened = ref(false)
watch(previewing, (v) => v && (previewOpened.value = true))
watch(() => chat.sessionId, () => { previewing.value = null })
const sources = computed(() => {
  const unique = new Map<string, SourceRef>()
  for (const p of props.message.parts) if (p.type === 'sources') for (const s of p.sources) unique.set(s.id, s)
  return [...unique.values()]
})
// 后续回答可以引用较早轮次检索到的来源,仍只允许打开当前会话已登记的引用。
const knownSources = computed(() => {
  const unique = new Map(sources.value.map((s) => [s.id, s]))
  for (const m of chat.messages) for (const p of m.parts) {
    if (p.type === 'sources') for (const s of p.sources) unique.set(s.id, s)
  }
  return unique
})

const items = computed(() => groupParts(props.message.parts))

/** 只有已落库、可回退的用户消息才能编辑或重新生成,且生成中不可用 */
const canRewind = computed(() => isUser.value && !!props.message.rewindable && !chat.running)
const userText = computed(() => {
  const first = props.message.parts[0]
  return first?.type === 'text' ? first.text : ''
})

/** 提问卡片只有在整段对话的最后一条消息、且没有在生成时才可作答 */
const askActive = computed(() => !chat.running && chat.messages[chat.messages.length - 1]?.id === props.message.id)

/** 整条消息的正文(不含步骤),用于"复制" */
const plainText = computed(() =>
  props.message.parts
    .filter((p) => p.type === 'text')
    .map((p) => (p as { text: string }).text)
    .join('\n\n')
    .trim(),
)
const canCopy = computed(() => !props.live && !!plainText.value)

const copyLabel = ref('复制')
function flashLabel(ok: boolean, set: (t: string) => void) {
  set(ok ? '已复制' : '复制失败')
  setTimeout(() => set('复制'), 1500)
}
async function copyMessage() {
  flashLabel(await copyText(plainText.value), (t) => (copyLabel.value = t))
}
/** 代码块的复制按钮是 v-html 里的原生元素,在容器上做事件委托 */
async function onMdClick(e: MouseEvent) {
  const link = (e.target as HTMLElement).closest<HTMLAnchorElement>('a')
  const href = link?.getAttribute('href') ?? ''
  if (href.startsWith('#source-')) {
    e.preventDefault()
    const source = knownSources.value.get(href.slice('#source-'.length))
    if (source) previewing.value = source
    return
  }
  const btn = (e.target as HTMLElement).closest<HTMLElement>('.code-copy')
  if (!btn) return
  const code = btn.closest('pre')?.querySelector('code')?.textContent ?? btn.closest('pre')?.textContent ?? ''
  flashLabel(await copyText(code), (t) => (btn.textContent = t))
}

const editing = ref(false)
const draft = ref('')
function startEdit() {
  draft.value = userText.value
  editing.value = true
}
async function confirmEdit() {
  const text = draft.value
  editing.value = false
  await chat.editAndResend(props.message.id, text)
}
// 发送被拒绝时消息会重新挂载,在这里取回改过的文字
watch(
  () => chat.rejectedEdit,
  (r) => {
    if (r?.id !== props.message.id) return
    draft.value = r.text
    editing.value = true
    chat.rejectedEdit = null
  },
  { immediate: true },
)
</script>

<template>
  <div class="row" :class="{ user: isUser }">
    <div v-if="!isUser" class="avatar" aria-hidden="true" />
    <div class="bubble" :class="{ user: isUser, editing }">
      <div v-if="message.attachments?.length" class="attach">
        <n-tag v-for="n in message.attachments" :key="n" size="small">{{ n }}</n-tag>
      </div>
      <template v-if="editing">
        <n-input v-model:value="draft" type="textarea" :autosize="{ minRows: 2, maxRows: 10 }" />
        <div class="actions">
          <n-button size="tiny" @click="editing = false">取消</n-button>
          <n-button size="tiny" type="primary" :disabled="!draft.trim()" @click="confirmEdit">发送</n-button>
        </div>
      </template>
      <template v-for="(it, i) in editing ? [] : items" :key="i">
        <div v-if="it.kind === 'text'" class="md" @click="onMdClick" v-html="renderMarkdown(it.text)" />
        <StepCard v-else-if="it.kind === 'step'" :step="it" :live="live" />
        <AskCard
          v-else-if="it.kind === 'ask'"
          :question="it.question"
          :kind="it.askKind"
          :options="it.options"
          :active="askActive"
          @answer="(t) => it.interruptId ? chat.send(t, [], undefined, it.interruptId) : chat.send(t, [])"
        />
        <DeliveredFiles v-else-if="it.kind === 'files'" :files="it.files" />
        <template v-else-if="it.kind === 'widgets'">
          <WidgetFrame v-for="(w, j) in it.widgets" :key="j" :widget="w" />
        </template>
      </template>
      <div v-if="sources.length" class="sources" aria-label="原文来源">
        <n-button v-for="source in sources" :key="source.id" size="tiny" @click="previewing = source">{{ sourceLabel(source) }}</n-button>
      </div>
      <n-spin v-if="live && !isUser" class="working" :size="16" />
      <n-alert v-if="message.status === 'error'" type="error" :show-icon="false" class="inline-error" role="alert">
        {{ message.errorText || '生成出错' }}
      </n-alert>
      <n-alert v-if="message.warningText" type="warning" :show-icon="false" class="inline-error" role="status">
        {{ message.warningText }}
      </n-alert>
      <n-text v-if="message.status === 'cancelled'" depth="3">(已停止生成)</n-text>
      <div v-if="(canRewind || canCopy) && !editing" class="actions hover">
        <n-button v-if="canCopy" size="tiny" quaternary @click="copyMessage">{{ copyLabel }}</n-button>
        <template v-if="canRewind">
          <n-button size="tiny" quaternary @click="startEdit">编辑</n-button>
          <n-button size="tiny" quaternary @click="chat.regenerate(message.id)">重新生成</n-button>
        </template>
      </div>
    </div>
    <SourcePreview v-if="previewOpened" :source="previewing" :session-id="chat.sessionId" @close="previewing = null" />
  </div>
</template>

<style scoped>
.row { display: flex; gap: 12px; padding: 10px 0; }
.row.user { justify-content: flex-end; padding-bottom: 20px; }
.avatar { width: 28px; height: 28px; border-radius: 9px; flex-shrink: 0; margin-top: 2px; background: linear-gradient(135deg, var(--accent), var(--accent-2)); }
.bubble { position: relative; max-width: 85%; min-width: 0; line-height: 1.75; font-size: 15px; }
/* 助手回复:整条放进一张卡片,步骤和正文都在里面,宽度占满 */
.bubble:not(.user) { flex: 1; max-width: none; background: var(--card-bg); border: 1px solid var(--line); border-radius: 4px 16px 16px 16px; padding: 14px 20px 16px; box-shadow: var(--shadow); }
.bubble:not(.user) > :first-child { margin-top: 0; }
.bubble:not(.user) > :last-child { margin-bottom: 0; }
.bubble.user { background: var(--accent); color: #fff; padding: 9px 16px; border-radius: 18px 18px 6px 18px; box-shadow: var(--shadow); }
.dark .bubble.user { color: #10121c; }
.inline-error { margin-top: 8px; }
.sources { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 12px; }
.working { display: block; margin: 8px 0 2px; }
.attach { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 6px; }
.bubble.editing { min-width: min(520px, 85vw); }
.actions { display: flex; justify-content: flex-end; gap: 6px; margin-top: 6px; }
/* 悬停(或键盘聚焦)时才显示操作,触屏设备没有悬停所以常显 */
.actions.hover { position: absolute; right: 0; top: 100%; margin: 2px 0 0; opacity: 0; transition: opacity .15s; z-index: 1; }
.bubble:hover .actions.hover, .bubble:focus-within .actions.hover { opacity: 1; }
@media (hover: none) { .actions.hover { opacity: 1; } }
.bubble > .step + .md { margin-top: 14px; }
.md :deep(> :first-child) { margin-top: 0; }
.md :deep(> :last-child) { margin-bottom: 0; }
.md :deep(p) { margin: 8px 0; }
.md :deep(h1), .md :deep(h2), .md :deep(h3), .md :deep(h4) { line-height: 1.4; font-weight: 650; margin: 20px 0 8px; }
.md :deep(h1) { font-size: 20px; }
.md :deep(h2) { font-size: 18px; padding-bottom: 6px; border-bottom: 1px solid var(--line); }
.md :deep(h3) { font-size: 16px; }
.md :deep(h4) { font-size: 15px; }
.md :deep(ul), .md :deep(ol) { margin: 8px 0; padding-left: 1.5em; }
.md :deep(li) { margin: 3px 0; }
.md :deep(li::marker) { color: var(--accent); }
.md :deep(hr) { border: none; border-top: 1px solid var(--line); margin: 16px 0; }
.md :deep(table) { max-width: 100%; margin: 10px 0; }
.md :deep(pre) { position: relative; background: var(--app-bg); border: 1px solid var(--line); padding: 12px 14px; border-radius: 10px; overflow: auto; font-size: 13px; line-height: 1.6; margin: 10px 0; }
.md :deep(.code-copy) { position: absolute; top: 6px; right: 6px; font-size: 12px; line-height: 1; padding: 4px 8px; border-radius: 6px; border: 1px solid var(--line); background: var(--card-bg); color: var(--muted); cursor: pointer; opacity: 0; transition: opacity .15s; }
.md :deep(pre:hover .code-copy), .md :deep(.code-copy:focus-visible) { opacity: 1; }
@media (hover: none) { .md :deep(.code-copy) { opacity: 1; } }
.md :deep(code) { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; }
.md :deep(:not(pre) > code) { background: var(--soft); padding: 1px 6px; border-radius: 5px; font-size: .9em; }
.md :deep(table) { border-collapse: separate; border-spacing: 0; border: 1px solid var(--line); border-radius: 10px; overflow: hidden; font-size: 14px; }
.md :deep(th), .md :deep(td) { border-bottom: 1px solid var(--line); padding: 7px 12px; text-align: left; }
.md :deep(th) { background: var(--soft); font-weight: 600; }
.md :deep(tr:last-child td) { border-bottom: none; }
.md :deep(blockquote) { margin: 8px 0; padding: 2px 14px; border-left: 3px solid var(--accent); color: var(--muted); }
.md :deep(a) { color: var(--accent); }
</style>
