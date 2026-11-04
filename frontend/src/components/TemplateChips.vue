<script setup lang="ts">
import { ref } from 'vue'
import { loadTemplates, makeTemplate, saveTemplates, type PromptTemplate } from '@/utils/templates'

const props = defineProps<{ draft: string }>()
const emit = defineEmits<{ pick: [text: string] }>()

const list = ref<PromptTemplate[]>(loadTemplates())
const showAdd = ref(false)
const label = ref('')
const text = ref('')
const err = ref<string | null>(null)

function openAdd() {
  label.value = ''
  text.value = props.draft // 输入框里已有内容时直接带过来,方便"存为模板"
  err.value = null
  showAdd.value = true
}
function add() {
  const t = makeTemplate(label.value, text.value, list.value)
  if (typeof t === 'string') {
    err.value = t
    return
  }
  list.value = [...list.value, t]
  saveTemplates(list.value)
  showAdd.value = false
}
function remove(id: string) {
  list.value = list.value.filter((t) => t.id !== id)
  saveTemplates(list.value)
}
</script>

<template>
  <div class="chips" role="group" aria-label="常用指令">
    <span v-for="t in list" :key="t.id" class="chip">
      <button type="button" class="use" :title="t.text" @click="emit('pick', t.text)">{{ t.label }}</button>
      <button type="button" class="del" :aria-label="`删除模板 ${t.label}`" @click="remove(t.id)">×</button>
    </span>
    <button type="button" class="chip add" @click="openAdd">+ 自定义</button>
    <n-modal v-model:show="showAdd" preset="card" title="新建常用指令" style="width: min(480px, 92vw)">
      <n-space vertical>
        <n-input v-model:value="label" placeholder="名称,如:周报" maxlength="20" show-count />
        <n-input v-model:value="text" type="textarea" placeholder="指令内容" :autosize="{ minRows: 3, maxRows: 8 }" />
        <n-alert v-if="err" type="error" :show-icon="false" role="alert">{{ err }}</n-alert>
      </n-space>
      <template #footer>
        <n-space justify="end">
          <n-button @click="showAdd = false">取消</n-button>
          <n-button type="primary" @click="add">保存</n-button>
        </n-space>
      </template>
    </n-modal>
  </div>
</template>

<style scoped>
.chips { display: flex; flex-wrap: wrap; justify-content: center; gap: 8px; margin: 22px auto 0; max-width: 560px; }
.chip { display: inline-flex; align-items: center; border: 1px solid var(--line); border-radius: 999px; background: var(--card-bg); font-size: 13px; overflow: hidden; }
.chip button { font: inherit; color: inherit; background: none; border: 0; cursor: pointer; padding: 6px 12px; }
.chip .use:hover { color: var(--accent); }
.chip .del { padding: 6px 8px 6px 0; opacity: 0; color: var(--muted); }
.chip:hover .del, .chip:focus-within .del { opacity: 1; }
@media (hover: none) { .chip .del { opacity: 1; } }
.chip.add { padding: 0; color: var(--muted); border-style: dashed; cursor: pointer; }
.chip button:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
</style>
