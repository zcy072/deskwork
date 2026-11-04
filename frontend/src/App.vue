<script setup lang="ts">
import { darkTheme, useOsTheme } from 'naive-ui'
import { computed, defineAsyncComponent, nextTick, onMounted, onUnmounted, ref, watch, watchEffect } from 'vue'
import { onUnauthorized } from '@/api/client'
import ChatView from '@/components/ChatView.vue'
import FilesPanel from '@/components/FilesPanel.vue'
import Sidebar from '@/components/Sidebar.vue'
import { useChatStore } from '@/stores/chat'
import { installAttentionReset } from '@/utils/attention'
import { cssVars, naiveOverrides, parseThemeMode, type ThemeMode } from '@/utils/theme'

// 只在真正需要时才加载的弹窗,不进首屏包
const ApiKeyModal = defineAsyncComponent(() => import('@/components/ApiKeyModal.vue'))

const chat = useChatStore()
const os = useOsTheme()
function storedTheme(): string | null {
  try {
    return localStorage.getItem('theme')
  } catch {
    return null // 浏览器禁用了存储
  }
}
const mode = ref<ThemeMode>(parseThemeMode(storedTheme()))
const isDark = computed(() => (mode.value === 'auto' ? os.value === 'dark' : mode.value === 'dark'))

function cycleTheme() {
  mode.value = mode.value === 'auto' ? 'light' : mode.value === 'light' ? 'dark' : 'auto'
  try {
    localStorage.setItem('theme', mode.value)
  } catch {
    /* 存储不可用:本次会话内仍生效 */
  }
}
const showSide = ref(false)
const showFiles = ref(false)
const sideToggle = ref<{ $el: HTMLElement } | null>(null)
const filesToggle = ref<{ $el: HTMLElement } | null>(null)

const modeLabel = computed(() => ({ auto: '跟随系统', light: '浅色', dark: '深色' })[mode.value])

const overrides = computed(() => naiveOverrides(isDark.value))
// 写在 <html> 上而不是某个容器上:被传送到 body 的弹窗、抽屉也能读到变量
watchEffect(() => {
  const root = document.documentElement
  for (const [k, v] of Object.entries(cssVars(isDark.value))) root.style.setProperty(k, v)
  root.classList.toggle('dark', isDark.value)
})

function toggleSide() {
  showSide.value = !showSide.value
  showFiles.value = false
}
function toggleFiles() {
  showFiles.value = !showFiles.value
  showSide.value = false
}
function closeDrawers() {
  showSide.value = showFiles.value = false
}

// 抽屉打开后把焦点送进去;关闭后若焦点还在抽屉里(或已丢失),还给对应的开关按钮
watch([showSide, showFiles], async ([side, files], [wasSide, wasFiles]) => {
  await nextTick()
  const drawer = side ? '.side.left' : files ? '.side.right' : null
  if (drawer) {
    document.querySelector<HTMLElement>(`${drawer} button, ${drawer} input`)?.focus()
    return
  }
  const toggle = wasSide ? sideToggle.value : wasFiles ? filesToggle.value : null
  const active = document.activeElement
  if (toggle && (!active || active === document.body || active.closest('.side'))) toggle.$el.focus()
})

function onLayoutKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && (showSide.value || showFiles.value)) {
    e.stopPropagation()
    closeDrawers()
  }
}

const showKey = ref(false)
const keyOpened = ref(false)
watch(showKey, (v) => v && (keyOpened.value = true))

let removeAttentionReset: (() => void) | null = null
onMounted(() => {
  removeAttentionReset = installAttentionReset()
  // 401 说明服务端要 Key 而浏览器没有(或填错了),弹出填写框
  onUnauthorized(() => (showKey.value = true))
  chat.refreshConversations()
})
onUnmounted(() => {
  removeAttentionReset?.()
  onUnauthorized(null)
  chat.dispose()
})

// 换了 Key 就是换了一个用户:整页重载,丢掉上一个身份留在内存里的会话和消息
const reloadAfterKey = () => window.location.reload()
</script>

<template>
  <n-config-provider :theme="isDark ? darkTheme : null" :theme-overrides="overrides" :class="{ dark: isDark }">
    <n-global-style />
    <n-message-provider>
      <n-dialog-provider>
        <div class="layout" @keydown="onLayoutKeydown">
          <div class="topbar">
            <n-button ref="sideToggle" size="small" aria-controls="side-left" :aria-expanded="showSide" @click="toggleSide">会话</n-button>
            <span class="tt">Deskwork</span>
            <n-button ref="filesToggle" size="small" aria-controls="side-right" :aria-expanded="showFiles" @click="toggleFiles">文件</n-button>
          </div>
          <div v-if="showSide || showFiles" class="mask" @click="closeDrawers" />
          <Sidebar
            id="side-left"
            class="side left"
            :class="{ open: showSide }"
            :theme-label="modeLabel"
            @toggle-theme="cycleTheme"
            @edit-key="showKey = true"
            @click.capture="(e: Event) => (e.target as HTMLElement).closest('.title') && (showSide = false)"
          />
          <ChatView class="main" />
          <FilesPanel id="side-right" class="side right" :class="{ open: showFiles }" />
        </div>
        <ApiKeyModal v-if="keyOpened" v-model:show="showKey" @saved="reloadAfterKey" />
      </n-dialog-provider>
    </n-message-provider>
  </n-config-provider>
</template>

<style>
html, body, #app { height: 100%; margin: 0; }

/* 配色变量由 utils/theme.ts 写到 <html> 上;这里只放与主题无关的布局 */
.layout { display: flex; height: 100vh; height: 100dvh; background: var(--app-bg); }
.main { flex: 1; min-width: 0; }
.topbar, .mask { display: none; }

/* 窄屏:两侧栏收成抽屉,聊天区占满宽度 */
@media (max-width: 900px) {
  .layout { flex-direction: column; }
  .topbar { display: flex; align-items: center; justify-content: space-between; padding: 8px 12px; border-bottom: 1px solid var(--line); background: var(--panel-bg); }
  .tt { font-weight: 600; }
  .main { flex: 1; min-height: 0; }
  .side { position: fixed; top: 0; bottom: 0; z-index: 100; width: min(300px, 85vw) !important; background: var(--panel-bg); transition: transform .2s, visibility 0s; }
  .side.left { left: 0; transform: translateX(-100%); }
  .side.right { right: 0; transform: translateX(100%); }
  .side.open { transform: none; }
  /* 收起时内容不可聚焦、不进读屏树;延迟到动画结束再隐藏 */
  .side:not(.open) { visibility: hidden; transition: transform .2s, visibility 0s .2s; }
  .mask { display: block; position: fixed; inset: 0; z-index: 99; background: rgba(0,0,0,.4); }
}
@media (prefers-reduced-motion: reduce) {
  .side, .side:not(.open) { transition: none; }
}
</style>
