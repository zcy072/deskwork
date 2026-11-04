<script setup lang="ts">
import { ref, watch } from 'vue'
import { getApiKey, setApiKey } from '@/api/client'
import { isImeEnter } from '@/utils/keys'

const show = defineModel<boolean>('show', { required: true })
const emit = defineEmits<{ saved: [] }>()
const draft = ref('')

watch(show, (v) => v && (draft.value = getApiKey()), { immediate: true })

function save() {
  if (!draft.value.trim()) return
  setApiKey(draft.value.trim())
  show.value = false
  emit('saved')
}
</script>

<template>
  <n-modal v-model:show="show" preset="card" title="访问密钥" style="width: min(460px, 92vw)">
    <p class="hint">
      服务端需要 API Key。会话和长期记忆按 Key 隔离:换一个 Key 就看不到原来的数据。Key 只保存在本浏览器。
    </p>
    <n-input
      v-model:value="draft"
      type="password"
      show-password-on="click"
      placeholder="API Key"
      @keydown.enter="(e: KeyboardEvent) => !isImeEnter(e) && save()"
    />
    <template #footer>
      <n-button type="primary" :disabled="!draft.trim()" @click="save">保存</n-button>
    </template>
  </n-modal>
</template>

<style scoped>
.hint { margin: 0 0 12px; color: var(--muted); font-size: 13px; }
</style>
