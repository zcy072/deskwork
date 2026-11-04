<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useChatStore } from '@/stores/chat'
import { isNearBottom, isNearTop } from '@/utils/scroll'
import { isImeEnter } from '@/utils/keys'
import MessageBubble from './MessageBubble.vue'
import TemplateChips from './TemplateChips.vue'

const chat = useChatStore()
const draft = computed(() => chat.draftFor())
const text = computed({ get: () => draft.value.text, set: (v: string) => { draft.value.text = v } })
const lostFiles = computed(() => draft.value.lostFiles)
const files = computed({ get: () => draft.value.files, set: (v: File[]) => { draft.value.files = v } })
const scroller = ref<HTMLElement | null>(null)
const picker = ref<HTMLInputElement | null>(null)
const composer = ref<{ focus: () => void } | null>(null)
const pane = ref<HTMLElement | null>(null)

// 生成结束后输入框重新可用,把焦点还给它
watch(
  () => chat.running,
  async (now, before) => {
    if (before && !now) {
      await nextTick()
      // 用户正在侧栏改名、搜索或在别处输入时不抢焦点(抢走会让改名框失焦而提交半截标题)
      const active = document.activeElement
      if (!active || active === document.body || pane.value?.contains(active)) composer.value?.focus()
    }
  },
)

/** 点常用指令:填进输入框(已有内容则换行追加)并聚焦,由用户确认后再发送 */
function useTemplate(t: string) {
  text.value = text.value.trim() ? `${text.value}\n${t}` : t
  nextTick(() => composer.value?.focus())
}

function onPick(e: Event) {
  const input = e.target as HTMLInputElement
  files.value.push(...Array.from(input.files ?? []))
  input.value = ''
}

// 拖拽与粘贴:生成中输入框被禁用,同样不接收文件
const dragging = ref(false)
let dragDepth = 0
const hasFiles = (e: DragEvent) => !!e.dataTransfer?.types?.includes('Files')
function onDragEnter(e: DragEvent) {
  if (chat.running || !hasFiles(e)) return
  dragDepth++
  dragging.value = true
}
function onDragLeave() {
  if (dragDepth > 0 && --dragDepth === 0) dragging.value = false
}
function onDrop(e: DragEvent) {
  dragDepth = 0
  dragging.value = false
  if (chat.running) return
  files.value.push(...Array.from(e.dataTransfer?.files ?? []))
}
function onPaste(e: ClipboardEvent) {
  if (chat.running) return
  const pasted = Array.from(e.clipboardData?.files ?? [])
  if (pasted.length === 0) return // 纯文本照常粘贴
  e.preventDefault()
  // 截图粘贴出来的文件都叫 image.png,加时间戳和序号避免重名
  const stamp = new Date().toISOString().slice(0, 19).replace(/[-:T]/g, '')
  files.value.push(
    ...pasted.map((f, i) =>
      f.name && f.name !== 'image.png' ? f : new File([f], `screenshot-${stamp}-${i + 1}.png`, { type: f.type }),
    ),
  )
}
// 草稿文字随输入写入本地存储(稍作延迟),刷新后恢复;发送成功时 text 被清空,草稿随之删除
watch([text, () => files.value.map((f) => f.name).join('\n')], () => {
  // 重新选择过的同名附件不再算丢失
  const picked = new Set(files.value.map((f) => f.name))
  if (draft.value.lostFiles.some((n) => picked.has(n))) {
    draft.value.lostFiles = draft.value.lostFiles.filter((n) => !picked.has(n))
  }
  chat.queueDraftSave()
})
// 打字的草稿是攒一下再写的,页面隐藏或关闭前写掉
function onHidden() {
  if (document.visibilityState === 'hidden') chat.flushDrafts()
}
onMounted(() => {
  window.addEventListener('pagehide', chat.flushDrafts)
  document.addEventListener('visibilitychange', onHidden)
})
onBeforeUnmount(() => {
  window.removeEventListener('pagehide', chat.flushDrafts)
  document.removeEventListener('visibilitychange', onHidden)
  chat.flushDrafts()
})
function dismissLostFiles() {
  draft.value.lostFiles = []
  chat.persistDraft()
}
async function submit() {
  // 保存原草稿对象:send 返回时用户可能已切换会话,甚至删除了原会话。
  const submitted = draft.value
  const t = text.value
  const f = files.value
  if (!t.trim() && f.length === 0) return
  text.value = ''
  files.value = []
  draft.value.lostFiles = []
  stick = true
  const accepted = await chat.send(t, f)
  // 没被服务端接受(限流、已有任务在跑等):把输入还回去,别让用户重打
  if (!accepted) {
    submitted.text = submitted.text ? `${t}\n${submitted.text}` : t
    submitted.files = [...f, ...submitted.files.filter((file) => !f.includes(file))]
  }
}
function fmtTokens(n: number): string {
  return n >= 10000 ? `${(n / 10000).toFixed(1)} 万` : String(n)
}
const usageText = computed(() => {
  const u = chat.usage
  if (!u || u.llm_calls === 0) return ''
  return `本会话 ${u.llm_calls} 次模型调用 · ${fmtTokens(u.total_tokens)} tokens`
})
const usageTitle = computed(() =>
  Object.entries(chat.usage?.by_model ?? {})
    .map(([m, s]) => `${m}: 输入 ${s.input_tokens} / 输出 ${s.output_tokens}`)
    .join('\n'),
)

function onEnter(e: KeyboardEvent) {
  if (e.shiftKey || isImeEnter(e)) return
  e.preventDefault()
  submit()
}

// 用户往上翻看时不强行拉回底部;发送新消息或切换会话时重新跟随
let stick = true
function onScroll() {
  const el = scroller.value
  if (!el) return
  stick = isNearBottom(el)
  // 滚到顶部自动接入更早的消息;loadEarlier 在加载中或没有更早消息时不会重复请求
  if (isNearTop(el) && chat.hasEarlier && !chat.loadingEarlier && !chat.loadingConversation) loadEarlier()
}

// 在顶部接入更早的消息后,按高度差补偿 scrollTop,让用户停留在原来看的那条消息上
async function loadEarlier() {
  const el = scroller.value
  if (!el) return
  const prevHeight = el.scrollHeight
  const prevTop = el.scrollTop
  stick = false // 列表变长不该触发"跟随到底部"
  await chat.loadEarlier()
  await nextTick()
  el.scrollTop = prevTop + (el.scrollHeight - prevHeight)
}

// 流式正文是往最后一个 text part 里追加,part 数量不变,所以签名里要带上文本长度
watch(
  () => {
    const last = chat.messages[chat.messages.length - 1]
    const tail = last?.parts[last.parts.length - 1]
    const len = tail?.type === 'text' ? tail.text.length : 0
    return `${chat.sessionId}|${chat.messages.length}|${last?.parts.length ?? 0}|${len}`
  },
  async (now, before) => {
    if (now.split('|')[0] !== before?.split('|')[0]) stick = true
    if (!stick) return
    await nextTick()
    scroller.value?.scrollTo({ top: scroller.value.scrollHeight })
  },
  { flush: 'post' },
)
</script>

<template>
  <section
    ref="pane"
    class="chat"
    :class="{ dragging }"
    @dragenter.prevent="onDragEnter"
    @dragover.prevent
    @dragleave="onDragLeave"
    @drop.prevent="onDrop"
  >
    <div v-if="dragging" class="drop-hint" aria-hidden="true">松开以添加文件</div>
    <div ref="scroller" class="messages" @scroll.passive="onScroll">
      <div class="inner" role="log" aria-live="polite" aria-label="对话消息">
        <div v-if="chat.loadingConversation" class="loading-conv" role="status" aria-label="正在加载对话">
          <n-skeleton text :repeat="3" />
          <n-skeleton text style="width: 60%" />
        </div>
        <div v-else-if="chat.messages.length === 0" class="hero">
          <div class="logo" />
          <h1>今天想处理什么文件?</h1>
          <p>上传文档、表格或演示稿,告诉我你想怎么处理。我会在沙箱里完成并把结果交给你。</p>
          <TemplateChips :draft="text" @pick="useTemplate" />
        </div>
        <div v-if="chat.hasEarlier && !chat.loadingConversation" class="earlier">
          <n-button size="small" quaternary :loading="chat.loadingEarlier" @click="loadEarlier">加载更早消息</n-button>
        </div>
        <MessageBubble
          v-for="(m, i) in chat.messages"
          :key="m.id"
          :message="m"
          :live="chat.running && i === chat.messages.length - 1"
        />
        <n-alert v-if="chat.error" type="error" role="alert" closable style="margin-top: 8px" @close="chat.error = null">
          {{ chat.error }}
        </n-alert>
      </div>
    </div>
    <div class="composer">
      <div class="inner">
       <div class="card">
        <div v-if="lostFiles.length" class="lost" role="status">
          刷新前选择的附件没有保存,请重新选择:{{ lostFiles.join('、') }}
          <n-button size="tiny" quaternary @click="dismissLostFiles">知道了</n-button>
        </div>
        <div v-if="files.length" class="picked">
          <n-tag v-for="(f, i) in files" :key="i" closable size="small" @close="files.splice(i, 1)">{{ f.name }}</n-tag>
        </div>
        <n-input
          ref="composer"
          v-model:value="text"
          type="textarea"
          :autosize="{ minRows: 1, maxRows: 8 }"
          placeholder="给 Deskwork 发消息"
          :input-props="{ 'aria-label': '给 Deskwork 发消息' }"
          :disabled="chat.running"
          @keydown.enter="onEnter"
          @paste="onPaste"
        />
        <div v-if="usageText" class="usage" :title="usageTitle">{{ usageText }}</div>
        <div class="bar">
          <input ref="picker" type="file" multiple hidden @change="onPick" />
          <n-button :disabled="chat.running" @click="picker?.click()">添加文件</n-button>
          <span class="hint">Enter 发送 · Shift+Enter 换行</span>
          <n-button v-if="chat.running" type="warning" :loading="chat.cancelling" @click="chat.stop()">
            停止生成
          </n-button>
          <n-button v-else type="primary" :disabled="!text.trim() && files.length === 0" @click="submit">
            发送
          </n-button>
        </div>
       </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.chat { position: relative; display: flex; flex-direction: column; height: 100%; background: var(--app-bg); }
.drop-hint { position: absolute; inset: 8px; z-index: 5; display: flex; align-items: center; justify-content: center; border: 2px dashed var(--accent); border-radius: 16px; background: color-mix(in srgb, var(--app-bg) 85%, transparent); color: var(--accent); font-size: 16px; pointer-events: none; }
.messages { flex: 1; overflow-y: auto; }
.inner { max-width: 820px; margin: 0 auto; padding: 20px 20px 8px; box-sizing: border-box; }
.messages .inner { padding-bottom: 28px; }
.earlier { display: flex; justify-content: center; padding: 8px 0; }
.loading-conv { margin-top: 24px; display: flex; flex-direction: column; gap: 10px; }
.hero { text-align: center; margin-top: clamp(16px, 16vh, 140px); }
.hero .logo { width: 48px; height: 48px; margin: 0 auto 18px; border-radius: 15px; background: linear-gradient(135deg, var(--accent), var(--accent-2)); box-shadow: var(--shadow); }
.hero h1 { margin: 0 0 8px; font-size: 26px; font-weight: 650; letter-spacing: .2px; }
.hero p { margin: 0 auto; max-width: 420px; color: var(--muted); line-height: 1.7; font-size: 14px; }
.composer { padding: 0 0 calc(16px + env(safe-area-inset-bottom, 0px)); }
.composer .inner { padding-left: max(20px, env(safe-area-inset-left, 0px)); padding-right: max(20px, env(safe-area-inset-right, 0px)); }
.composer .inner { padding-top: 0; }
.card { background: var(--card-bg); border: 1px solid var(--line); border-radius: 16px; box-shadow: var(--shadow); padding: 10px 12px 10px; transition: border-color .15s, box-shadow .15s; }
.card:focus-within { border-color: var(--accent); }
.card :deep(.n-input) { --n-border: none !important; --n-border-hover: none !important; --n-border-focus: none !important; --n-box-shadow-focus: none !important; background: transparent !important; }
.card :deep(.n-input__border), .card :deep(.n-input__state-border) { display: none; }
.lost { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-bottom: 8px; font-size: 12px; color: var(--muted); }
.picked { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 8px; }
.usage { margin-top: 6px; font-size: 12px; color: var(--muted); }
.hint { flex: 1; text-align: center; font-size: 12px; color: var(--muted); }
@media (max-width: 900px) { .hint { display: none; } }
.bar { display: flex; justify-content: space-between; align-items: center; margin-top: 8px; }
</style>
