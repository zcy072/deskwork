<script setup lang="ts">
import { ref } from 'vue'

const props = defineProps<{
  question: string
  kind: 'single_select' | 'multi_select' | 'free_text'
  options: string[]
  /** 可作答:这是对话里最后一条消息且当前没有在生成 */
  active: boolean
}>()
const emit = defineEmits<{ answer: [text: string] }>()

const picked = ref<string[]>([])
const text = ref('')

/** 跳过也是一种回答,明确告诉模型别再追问、按合理默认继续 */
const SKIP_ANSWER = '(用户跳过了这个问题,请按合理的默认选择继续,不要再追问同一件事)'

function submit() {
  const answer = props.kind === 'free_text' ? text.value.trim() : picked.value.join('、')
  if (answer) emit('answer', answer)
}
</script>

<template>
  <n-card size="small" class="ask" :bordered="true">
    <div class="q">{{ question }}</div>
    <template v-if="kind === 'single_select'">
      <n-space vertical>
        <n-button v-for="o in options" :key="o" block secondary :disabled="!active" @click="emit('answer', o)">
          {{ o }}
        </n-button>
      </n-space>
    </template>
    <template v-else-if="kind === 'multi_select'">
      <n-checkbox-group v-model:value="picked" :disabled="!active">
        <n-space vertical>
          <n-checkbox v-for="o in options" :key="o" :value="o" :label="o" />
        </n-space>
      </n-checkbox-group>
      <n-button class="go" size="small" type="primary" :disabled="!active || !picked.length" @click="submit">
        确认
      </n-button>
    </template>
    <template v-else>
      <n-input v-model:value="text" type="textarea" :autosize="{ minRows: 2, maxRows: 6 }" :disabled="!active" />
      <n-button class="go" size="small" type="primary" :disabled="!active || !text.trim()" @click="submit">
        发送
      </n-button>
    </template>
    <n-button v-if="active" class="skip" size="tiny" quaternary @click="emit('answer', SKIP_ANSWER)">
      跳过
    </n-button>
    <n-text v-else depth="3" class="done">已回答或已跳过</n-text>
  </n-card>
</template>

<style scoped>
.ask { margin: 10px 0; max-width: 440px; border-radius: 14px; box-shadow: var(--shadow); border-left: 3px solid var(--accent); }
.q { font-weight: 600; margin-bottom: 8px; }
.go { margin-top: 8px; }
.skip { margin-top: 6px; display: block; }
.done { display: block; margin-top: 6px; font-size: 12px; }
</style>
